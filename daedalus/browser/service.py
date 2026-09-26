"""The browser service: every environment's browser daemon behind one interface, mirrored in the database.

The daemon owns Chromium, its profiles and tabs; this service owns what the host knows about them —
whose group is whose, which project's cookies it uses, who holds its controls, how it ended — and
keeps that true across its own restarts, which the daemons outlive. On every connection it
reconciles: a group the daemon no longer lists closed while nobody was looking, or went with a daemon
that restarted (``lost``); one the daemon runs with no row is adopted from the labels it was opened
with. Between reconciles it follows the daemon's events.

It is also where a person's hand on the browser meets the agent: taking and giving back control, an
agent's handoff, the "needs you" a page raises, and the one wake the owner gets when the browser is
given back.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus import load as load_math
from daedalus.browser import wire
from daedalus.browser.model import (
    CONTROL_OWNERS,
    ENVS,
    EPHEMERAL,
    GROUP_STATUSES,
    BrowserError,
    BrowserGone,
    EnvUnavailable,
    InvalidRequest,
    NotFound,
    OverCap,
    Owner,
    group_id,
    profile_id,
    rpc_failure,
    valid_id,
)
from daedalus.browser.owners import BrowserOwners
from daedalus.terminals.client import Channel, PtydClient, Unavailable
from daedalus.terminals.service import iso, now_iso

if TYPE_CHECKING:
    from daedalus.config import BrowserConfig
    from daedalus.host.events import EventBus
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

GroupView = dict[str, Any]
"""A group as the API and the tools see it; the shape is in ``docs/architecture/browser.md``."""

RECONNECT_FIRST = 0.5
RECONNECT_MAX = 10.0
SYNC_SECONDS = 60.0
PRUNE_SECONDS = 3600.0
FLUSH_SECONDS = 2.0
COSTS_SAVE_SECONDS = 300.0
ADMISSION_RECHECK = 2.0
"""How often an agent waiting for a free browser tries again without being woken: a browser closed
on another environment, or by the daemon's own idle close before its event arrived, wakes nobody."""
CALL_SLACK = 10.0
"""What a call's own timeout exceeds the daemon's wait by, so the daemon's answer always arrives first."""
DOWNLOAD_CHUNK = 512 << 10
UPLOAD_CHUNK = 384 << 10
"""What one ``upload.put`` carries: base64 of it stays under the daemon's 512 KiB and one frame."""
PROFILE = "browser"
"""The cost profile of a browser in the load estimate, beside the terminals' ``shell`` and CLIs."""
OUTDATED = "restart the browser service, which ends its browsers"
LABEL = "browser service"


@dataclass(slots=True)
class Link:
    """One environment: where its daemon is, and what is known of it now."""

    env: str
    run_dir: Path | None
    client: PtydClient | None = None
    reason: str = "not_configured"
    detail: str = ""
    info: dict[str, Any] = field(default_factory=dict)
    event_seq: int = 0
    event_seq_saved: int = 0
    connected_once: asyncio.Event = field(default_factory=asyncio.Event)
    stats: dict[str, Any] = field(default_factory=dict)
    """The daemon's newest ``browser.stats``, which it publishes every ten seconds while a browser runs."""

    @property
    def available(self) -> bool:
        return self.client is not None and self.client.connected

    @property
    def instance(self) -> str:
        return self.client.instance if self.client is not None else ""


@dataclass(frozen=True, slots=True)
class Attachment:
    """One live view's channel to one group, as the daemon opened it."""

    group_id: str
    env: str
    client_id: str
    channel: Channel
    client: PtydClient

    @property
    def service_alive(self) -> bool:
        return self.client.connected


@dataclass(slots=True)
class Waiter:
    actor: str
    env: str
    group_id: str
    since: str

    def view(self) -> dict[str, Any]:
        return {"actor": self.actor, "env": self.env, "group_id": self.group_id, "since": self.since}


Subscriber = Callable[[str, str, dict[str, Any]], Awaitable[None]]
"""``(env, type, data)`` for every daemon event after the service has handled it."""
Wake = Callable[[Owner, str], Awaitable[None]]
"""How an owner is told something without being asked: a message into its session, or to its CLI."""


def _count(tabs: Any) -> int:
    """A group's tabs as the daemon gives them: their number, or the list of them."""
    if isinstance(tabs, bool):
        return 0
    if isinstance(tabs, int):
        return max(0, tabs)
    return len(tabs) if isinstance(tabs, list) else 0


def _stamp_after(hours: float = 0, days: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours, days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Browsers:
    def __init__(
        self,
        db: Database,
        *,
        run_dirs: dict[str, Path | None],
        config: Callable[[], BrowserConfig],
        owners: BrowserOwners,
        bus: EventBus | None = None,
        wake: Wake | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.owners = owners
        self.bus = bus
        self.wake = wake
        self.links = {env: Link(env, run_dirs.get(env)) for env in ENVS}
        for link in self.links.values():
            if link.run_dir is not None:
                link.reason, link.detail = "connecting", "the first connection is being made"
        self.costs = load_math.ProfileCosts()
        self._costs_dirty = False
        self._costs_saved_at = 0.0
        self._notes: dict[str, tuple[str, str]] = {}
        """``group → (note, by)``: what the operator said when giving a browser back, kept for the
        control event that carries the give-back to the owner. The event, not the request, wakes the
        owner, so a give-back is told once however it happened — the button, or the hold running out."""
        self._freed = asyncio.Event()
        self._waiting: list[Waiter] = []
        self._subscribers: list[Subscriber] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._state_lock = asyncio.Lock()
        """Taken by every change of a group's status, so a reconcile and an event cannot both close
        one and publish it twice."""
        self._closing = False

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        """Connect to every configured environment in the background; never waits for a daemon."""
        saved = await self.db.kv_get("browser.profile_costs", {})
        if isinstance(saved, dict):
            self.costs.load(saved)
        for link in self.links.values():
            if link.run_dir is not None:
                self._tasks.append(asyncio.create_task(self._connection_loop(link), name=f"browser-{link.env}"))
        self._tasks.append(asyncio.create_task(self._housekeeping(), name="browser-housekeeping"))
        return list(self._tasks)

    async def close(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for link in self.links.values():
            if link.client is not None:
                await link.client.close()
        with contextlib.suppress(Exception):
            await self._flush_cursors()
            await self._save_costs(force=True)

    async def wait_available(self, env: str, timeout: float = 5.0) -> bool:
        """Wait until the environment has been connected and reconciled once; for tests and the doctor."""
        link = self.links[env]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(link.connected_once.wait(), timeout)
        return link.available

    async def _connection_loop(self, link: Link) -> None:
        assert link.run_dir is not None
        delay = RECONNECT_FIRST
        while not self._closing:

            async def notified(method: str, params: dict[str, Any], env: str = link.env) -> None:
                await self._on_notification(env, method, params)

            client = PtydClient(link.env, link.run_dir, on_notification=notified, label=LABEL, outdated=OUTDATED, lock=wire.LOCK_FILE)
            try:
                await client.connect()
                link.info = await client.call("daemon.info")
                link.client = client
                link.reason, link.detail = "", ""
                await self._resume(link)
                link.connected_once.set()
                delay = RECONNECT_FIRST
                chromium = link.info.get("chromium") or {}
                logger.info("browser service %s connected: %s, Chromium %s (instance %s)", link.env, link.info.get("version"), chromium.get("version") or "none", client.instance)
                await client.wait_closed()
                link.reason, link.detail = "unreachable", client.lost_reason or "the connection closed"
                logger.warning("browser service %s went away: %s", link.env, link.detail)
            except Unavailable as exc:
                if (link.reason, link.detail) != (exc.reason, exc.detail):
                    logger.info("browser service %s unavailable: %s", link.env, exc.detail)
                link.reason, link.detail = exc.reason, exc.detail
            except wire.RpcError as exc:
                link.reason, link.detail = "unreachable", f"the browser service refused {exc.message}"
                logger.warning("browser service %s: %s", link.env, link.detail)
            except asyncio.CancelledError:
                await client.close()
                raise
            except Exception:  # noqa: BLE001 — a bug in reconcile must not end the reconnecting for good
                logger.exception("browser service %s: connecting failed", link.env)
                link.reason, link.detail = "unreachable", "connecting failed; the log has the error"
            link.client = None
            await client.close()
            link.connected_once.set()
            self._wake_waiters()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _resume(self, link: Link) -> None:
        assert link.client is not None
        saved = await self.db.kv_get(f"browser.{link.env}.events", {})
        after = int(saved.get("seq") or 0) if isinstance(saved, dict) and saved.get("instance") == link.client.instance else 0
        link.event_seq = link.event_seq_saved = after
        # Subscribe first and list second: an event between the two is seen twice rather than never.
        await link.client.call("events.subscribe", {"after_seq": after})
        await self._reconcile(link)

    async def _housekeeping(self) -> None:
        last_sync = last_prune = time.monotonic()
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await self._flush_cursors()
                await self._save_costs()
                self._wake_waiters()
                now = time.monotonic()
                if now - last_sync >= SYNC_SECONDS:
                    last_sync = now
                    for link in self.links.values():
                        if link.available:
                            await self._reconcile(link)
                if now - last_prune >= PRUNE_SECONDS:
                    last_prune = now
                    await self.prune()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — housekeeping failing once must not stop it for good
                logger.exception("browser housekeeping failed")

    async def _flush_cursors(self) -> None:
        for link in self.links.values():
            if link.client is not None and link.event_seq != link.event_seq_saved:
                await self.db.kv_set(f"browser.{link.env}.events", {"instance": link.client.instance, "seq": link.event_seq})
                link.event_seq_saved = link.event_seq

    async def _save_costs(self, *, force: bool = False) -> None:
        if self._costs_dirty and (force or time.monotonic() - self._costs_saved_at >= COSTS_SAVE_SECONDS):
            await self.db.kv_set("browser.profile_costs", self.costs.dump())
            self._costs_dirty = False
            self._costs_saved_at = time.monotonic()

    # -- environments ---------------------------------------------------------------------------

    def configured(self, env: str | None = None) -> bool:
        """Whether this installation has a browser daemon for ``env`` (any environment without one)."""
        if env is None:
            return any(link.run_dir is not None for link in self.links.values())
        link = self.links.get(env)
        return link is not None and link.run_dir is not None

    def available(self, env: str) -> bool:
        link = self.links.get(env)
        return link is not None and link.available

    def agent_env(self) -> str:
        """Where an agent's browser runs: the configured one, else the container's where there is one."""
        wanted = self.config().env
        if wanted in ENVS:
            return wanted
        return "container" if self.configured("container") else "host"

    def environments(self) -> list[dict[str, Any]]:
        out = []
        for env, link in self.links.items():
            info = link.info if link.available else {}
            chromium = info.get("chromium") or {}
            capabilities = info.get("capabilities") or {}
            out.append(
                {
                    "env": env,
                    "configured": link.run_dir is not None,
                    "available": link.available,
                    "reason": "" if link.available else link.reason,
                    "detail": "" if link.available else link.detail,
                    "version": str(info.get("version") or ""),
                    "chromium": {"version": str(chromium.get("version") or ""), "kind": str(chromium.get("kind") or ""), "error": str(chromium.get("error") or "")} if info else None,
                    "sandbox": str(capabilities.get("sandbox") or "") if info else "",
                    "limits": info.get("limits") or {},
                    "counts": info.get("counts") or {},
                }
            )
        return out

    def _link(self, env: str) -> Link:
        link = self.links.get(env)
        if link is None:
            raise InvalidRequest(f"an environment is one of {', '.join(ENVS)}, not {env!r}")
        return link

    def _client(self, env: str) -> PtydClient:
        link = self._link(env)
        if link.run_dir is None:
            raise EnvUnavailable(f"this installation has no browser in the {env} environment", env=env, reason="not_configured")
        if not link.available or link.client is None:
            raise EnvUnavailable(f"the {env} browser service is not available: {link.detail or link.reason}", env=env, reason=link.reason)
        return link.client

    async def _call(self, env: str, method: str, params: dict[str, Any], *, what: str, timeout: float = 10.0) -> Any:
        client = self._client(env)
        try:
            return await client.call(method, params, timeout=timeout)
        except Unavailable as exc:
            raise EnvUnavailable(f"{what}: the {env} browser service is not available: {exc.detail}", env=env, reason=exc.reason) from None
        except wire.RpcError as exc:
            raise rpc_failure(exc, what) from None

    # -- rows -------------------------------------------------------------------------------

    async def _row(self, group: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM browser_groups WHERE id = ?", (group,))
        if row is None:
            raise NotFound(f"no browser {group}")
        return dict(row)

    @staticmethod
    def owner_of(row: dict[str, Any]) -> Owner:
        return Owner(row["owner_kind"], row["owner_id"], project_id=row["project_id"], session_id=row["session_id"], staff_id=row["staff_id"])

    def _view(self, row: dict[str, Any], label: str = "") -> GroupView:
        return {
            "id": row["id"],
            "env": row["env"],
            "profile": row["profile"],
            "browser_id": row["browser_id"],
            "owner": {"kind": row["owner_kind"], "id": row["owner_id"], "label": label},
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "staff_id": row["staff_id"],
            "fresh": bool(row["fresh"]),
            "status": row["status"],
            "close_reason": row["close_reason"],
            "control": {"owner": row["control_owner"], "reason": row["control_reason"]},
            "url": row["url"],
            "title": row["title"],
            "tabs": row["tabs"],
            "created_at": row["created_at"],
            "last_activity_at": row["last_activity_at"],
            "closed_at": row["closed_at"],
        }

    async def get(self, group: str, *, live: bool = True) -> GroupView:
        """One group; with ``live``, its tabs and controls as the daemon has them now."""
        row = await self._row(group)
        view = self._view(row, await self.owners.label(self.owner_of(row)))
        view["live"] = None
        if live and row["status"] == "open" and self.available(row["env"]):
            with contextlib.suppress(BrowserError):
                listing = await self._call(row["env"], "tab.list", {"group_id": group}, what="listing the tabs")
                view["live"] = {"tabs": listing.get("tabs") or [], "active_tab": listing.get("active_tab")}
        return view

    async def list(self, *, session_id: str | None = None, staff_id: str | None = None, project_id: str | None = None, status: str | None = None) -> builtins.list[GroupView]:
        where: builtins.list[str] = []
        params: builtins.list[Any] = []
        for column, value in (("session_id", session_id), ("staff_id", staff_id), ("project_id", project_id)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if status is not None:
            if status not in GROUP_STATUSES:
                raise InvalidRequest(f"a status is one of {', '.join(GROUP_STATUSES)}")
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM browser_groups" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = [dict(r) for r in await self.db.fetchall(sql + " ORDER BY status != 'open', last_activity_at DESC", params)]  # noqa: S608 — column names are literals above
        return [self._view(r, await self.owners.label(self.owner_of(r))) for r in rows]

    async def audit(self, group: str, env: str, actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
        """Append to the audit. Never with what anyone typed: an agent's text is a length and a hash,
        a person's input a count, because a search box holds what a person would not have written down."""
        await self.db.execute(
            "INSERT INTO browser_audit(at, group_id, env, actor, action, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), group, env, actor, action, json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))),
        )

    async def audit_log(self, group: str, *, limit: int = 200) -> builtins.list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM browser_audit WHERE group_id = ? ORDER BY seq DESC LIMIT ?", (group, max(1, min(limit, 1000))))
        return [{"seq": r["seq"], "at": r["at"], "env": r["env"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail_json"] or "{}")} for r in rows]

    async def prune(self) -> tuple[int, int]:
        cfg = self.config()
        closed_before = _stamp_after(hours=cfg.closed_retention_hours)
        audit_before = _stamp_after(days=cfg.audit_retention_days)
        old = await self.db.fetchone("SELECT count(*) AS n FROM browser_groups WHERE status != 'open' AND COALESCE(closed_at, created_at) < ?", (closed_before,))
        await self.db.execute("DELETE FROM browser_groups WHERE status != 'open' AND COALESCE(closed_at, created_at) < ?", (closed_before,))
        audit = await self.db.fetchone("SELECT count(*) AS n FROM browser_audit WHERE at < ?", (audit_before,))
        await self.db.execute("DELETE FROM browser_audit WHERE at < ?", (audit_before,))
        await self.db.execute("DELETE FROM browsers WHERE status != 'running' AND COALESCE(exited_at, started_at) < ?", (closed_before,))
        return int(old["n"]) if old else 0, int(audit["n"]) if audit else 0

    async def _publish(self, event_type: str, payload: dict[str, Any], row: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.publish(event_type, payload, project_id=row.get("project_id"), session_id=row.get("session_id"), staff_id=row.get("staff_id"))
        except Exception:  # noqa: BLE001 — a browser's life does not depend on who hears of it
            logger.exception("publishing %s for browser %s failed", event_type, row.get("id"))

    # -- opening ----------------------------------------------------------------------------

    def queue(self) -> builtins.list[dict[str, Any]]:
        """Agents waiting for a free browser, oldest first."""
        return [w.view() for w in self._waiting]

    def _wake_waiters(self) -> None:
        if self._waiting:
            self._freed.set()
            self._freed = asyncio.Event()

    async def open(self, owner: Owner, *, url: str | None = None, fresh: bool = False, actor: str, viewport: dict[str, int] | None = None, wait: float | None = None) -> dict[str, Any]:
        """Open the owner's group (starting its profile's browser when it is not running) and return
        ``{group, tab, created}``; an open group is returned as it is, with its active tab.

        Past the environment's cap an agent waits in line up to ``wait`` seconds (the configured wait
        by default) for a browser to close; the operator is refused at once with ``over_cap``,
        because the daemon never closes a browser to make room and neither does the host.
        """
        env = self.agent_env()
        self._client(env)
        if not await self.owners.exists(owner):
            raise NotFound(f"no {owner.kind} {owner.id}")
        group = group_id(owner, fresh=fresh)
        profile = profile_id(owner, fresh=fresh)
        agent = actor != "operator"
        deadline = time.monotonic() + (self.config().agent_wait_seconds if wait is None else wait)
        params: dict[str, Any] = {"group_id": group, "profile": profile, "labels": owner.labels()}
        if url:
            params["url"] = url
        if viewport:
            params["viewport"] = viewport
        waiter: Waiter | None = None
        try:
            while True:
                freed = self._freed
                try:
                    result = await self._call(env, "browser.open", params, what="opening the browser", timeout=45.0)
                    break
                except OverCap as exc:
                    if not agent:
                        raise
                    if waiter is None:
                        waiter = Waiter(actor=actor, env=env, group_id=group, since=now_iso())
                        self._waiting.append(waiter)
                        logger.info("browser for %s waits for a free place: %s", actor, exc.message)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        limit = exc.details.get("max") or self.config().running_cap
                        raise OverCap(
                            f"all browsers are busy ({limit} of {limit}) and none came free in time; close one of yours with BrowserClose, or ask the operator",
                            waited=True,
                            max=limit,
                        ) from None
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(freed.wait(), min(remaining, ADMISSION_RECHECK))
        finally:
            if waiter is not None and waiter in self._waiting:
                self._waiting.remove(waiter)
        group_info = result.get("group") or {}
        tab = result.get("tab") or {}
        created = bool(result.get("created"))
        now = now_iso()
        link = self.links[env]
        async with self._state_lock:
            existing = await self.db.fetchone("SELECT status FROM browser_groups WHERE id = ?", (group,))
            revived = existing is None or existing["status"] != "open"
            await self.db.execute(
                "INSERT INTO browser_groups(id, env, profile, browser_id, owner_kind, owner_id, project_id, session_id, staff_id, fresh, status, close_reason, "
                "control_owner, control_reason, url, title, tabs, daemon_instance, created_by, created_at, last_activity_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(id) DO UPDATE SET env = excluded.env, profile = excluded.profile, browser_id = excluded.browser_id, status = 'open', close_reason = '', "
                "control_owner = excluded.control_owner, control_reason = excluded.control_reason, url = excluded.url, title = excluded.title, tabs = excluded.tabs, "
                "daemon_instance = excluded.daemon_instance, last_activity_at = excluded.last_activity_at, closed_at = NULL, "
                "created_at = CASE WHEN browser_groups.status = 'open' THEN browser_groups.created_at ELSE excluded.created_at END, "
                "created_by = CASE WHEN browser_groups.status = 'open' THEN browser_groups.created_by ELSE excluded.created_by END",
                (
                    group, env, profile, str(group_info.get("browser_id") or ""), owner.kind, owner.id, owner.project_id, owner.session_id, owner.staff_id, int(fresh),
                    self._control_owner(group_info), str((group_info.get("control") or {}).get("reason") or ""),
                    str(tab.get("url") or url or ""), str(tab.get("title") or ""), _count(group_info.get("tabs")) or 1, link.instance, actor, now, now,
                ),
            )
            await self.db.execute(
                "INSERT INTO browser_profiles(id, env, scope, project_id, session_id, staff_id, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(env, id) DO UPDATE SET last_used_at = excluded.last_used_at",
                (profile, env, EPHEMERAL if fresh else ("project" if owner.project_id else owner.kind), owner.project_id, owner.session_id, owner.staff_id, now, now),
            )
        row = await self._row(group)
        if created or revived:
            await self.audit(group, env, actor, "open", {"profile": profile, "fresh": fresh, "url": url or "", "browser_id": row["browser_id"]})
            await self._publish("browser.opened", {"group_id": group, "env": env, "profile": profile, "owner_kind": owner.kind, "owner_id": owner.id, "url": row["url"], "fresh": fresh}, row)
        return {"group": self._view(row, await self.owners.label(owner)), "tab": tab, "created": created}

    @staticmethod
    def _control_owner(group_info: dict[str, Any]) -> str:
        owner = str((group_info.get("control") or {}).get("owner") or "agent")
        return owner if owner in CONTROL_OWNERS else "agent"

    async def find(self, owner: Owner, *, fresh: bool = False) -> dict[str, Any] | None:
        """The owner's open group, or ``None`` when it has none open now."""
        row = await self.db.fetchone("SELECT * FROM browser_groups WHERE id = ? AND status = 'open'", (group_id(owner, fresh=fresh),))
        return dict(row) if row is not None else None

    # -- calls on a group -------------------------------------------------------------------

    async def call(self, group: str, method: str, params: dict[str, Any] | None = None, *, what: str, timeout: float = 10.0) -> Any:
        """A daemon call about an open group; a closed or lost one is ``BrowserGone``.

        The call's own timeout is the caller's plus the slack, so a daemon that answers ``timeout``
        itself is heard rather than cut off.
        """
        row = await self._row(group)
        if row["status"] != "open":
            raise BrowserGone(self._gone_text(row), reason=row["close_reason"] or row["status"])
        try:
            result = await self._call(row["env"], method, dict(params or {}), what=what, timeout=timeout + CALL_SLACK)
        except BrowserGone:
            await self._mark_closed(row, "closed", status="closed")
            raise
        except NotFound:
            if not await self._still_listed(row):
                # The daemon forgot the group while this host was not looking: its browser closed idle.
                await self._mark_closed(row, "idle", status="closed")
                raise BrowserGone(self._gone_text({**row, "close_reason": "idle"}), reason="idle") from None
            raise
        await self.db.execute("UPDATE browser_groups SET last_activity_at = ? WHERE id = ?", (now_iso(), group))
        return result

    async def _still_listed(self, row: dict[str, Any]) -> bool:
        """Whether the daemon still has the group: a "not found" is then about something in it (a
        download, a dialog), not about the group itself."""
        try:
            listing = await self._call(row["env"], "group.list", {}, what="listing the groups")
        except BrowserError:
            return True  # nothing learned; the refusal stands as it came
        return any(str(g.get("id")) == row["id"] for g in listing.get("groups") or [] if isinstance(g, dict))

    @staticmethod
    def _gone_text(row: dict[str, Any]) -> str:
        reason = row.get("close_reason") or row.get("status") or "closed"
        why = {
            "idle": "it was closed after ten minutes without use",
            "crashed": "it crashed",
            "memory": "it used more memory than a browser may",
            "lost": "the browser service restarted",
            "shutdown": "the browser service stopped",
            "owner_gone": "its owner is gone",
        }.get(str(reason), "it was closed")
        return f"this browser is not open: {why}. Its profile and logins are kept; BrowserOpen starts it again"

    def origin(self, *, actor: str, launch_id: str = "") -> dict[str, Any]:
        """The ``origin`` an agent's call on a page carries, with the configured wait for a person."""
        wait_ms = int(self.config().control_wait_seconds * 1000)
        origin: dict[str, Any] = {"actor": "operator" if actor == "operator" else "agent", "wait_ms": wait_ms}
        if launch_id:
            origin["launch_id"] = launch_id
        return origin

    # -- control ----------------------------------------------------------------------------

    async def control(self, group: str, owner: str, *, client_id: str = "", ttl_ms: int | None = None, reason: str = "", note: str = "", by: str = "operator") -> dict[str, Any]:
        """Take the browser (``human``, naming the live-view client that drives), pause the agent, or
        give it back (``agent``, with an optional note for the agent). The owner hears of a give-back
        once, from the daemon's own ``control`` event."""
        if owner not in CONTROL_OWNERS:
            raise InvalidRequest(f"control is one of {', '.join(CONTROL_OWNERS)}, not {owner!r}")
        row = await self._row(group)
        if row["status"] != "open":
            raise BrowserGone(self._gone_text(row))
        params: dict[str, Any] = {"group_id": group, "owner": owner}
        if client_id:
            params["client_id"] = client_id
        if ttl_ms is not None:
            params["ttl_ms"] = ttl_ms
        if reason:
            params["reason"] = reason[:300]
        if owner == "agent" and row["control_owner"] != "agent":
            self._notes[group] = (note.strip()[:2000], by)
        result = await self._call(row["env"], "control.set", params, what="handing the browser over")
        await self.audit(group, row["env"], by, {"human": "take", "paused": "pause", "agent": "give"}[owner], {"client_id": client_id, "reason": reason[:300], "note_len": len(note.strip())})
        return result if isinstance(result, dict) else {}

    async def handoff(self, group: str, reason: str, what: str, *, actor: str) -> dict[str, Any]:
        """The agent asks the operator to take over: the group is paused with the reason, and the
        operator is told it needs them. Returns what the tool tells the agent about where it stopped."""
        row = await self._row(group)
        if row["status"] != "open":
            raise BrowserGone(self._gone_text(row))
        await self._call(row["env"], "control.set", {"group_id": group, "owner": "paused", "reason": f"{reason}: {what}"[:300]}, what="handing the browser to the operator")
        await self.db.execute("UPDATE browser_groups SET control_owner = 'paused', control_reason = ? WHERE id = ?", (f"{reason}: {what}"[:300], group))
        await self.audit(group, row["env"], actor, "handoff", {"reason": reason, "what": what[:500]})
        await self._needs_you(row, reason, what, row["url"])
        return {"url": row["url"], "title": row["title"]}

    async def _needs_you(self, row: dict[str, Any], reason: str, what: str, url: str) -> None:
        label = await self.owners.label(self.owner_of(row))
        await self._publish("browser.needs_you", {"group_id": row["id"], "reason": reason, "what": what[:500], "url": url[:2000], "title": label}, row)

    # -- closing ----------------------------------------------------------------------------

    async def close_group(self, group: str, *, actor: str, reason: str = "closed") -> None:
        row = await self._row(group)
        if row["status"] == "open" and self.available(row["env"]):
            with contextlib.suppress(NotFound):
                await self._call(row["env"], "group.close", {"group_id": group}, what="closing the browser")
        await self._mark_closed(row, reason, status="closed", by=actor)

    async def close_owned(self, owner_kind: str, owner_id: str, *, actor: str = "system") -> int:
        """Close every open group of an owner that is going away; returns how many were open."""
        rows = await self.db.fetchall("SELECT id FROM browser_groups WHERE status = 'open' AND owner_kind = ? AND owner_id = ?", (owner_kind, owner_id))
        for row in rows:
            try:
                await self.close_group(row["id"], actor=actor, reason="owner_gone")
            except BrowserError as exc:
                logger.warning("browser %s of %s %s not closed: %s", row["id"], owner_kind, owner_id, exc.message)
        return len(rows)

    async def _mark_closed(self, row: dict[str, Any], reason: str, *, status: str, by: str = "system") -> bool:
        async with self._state_lock:
            current = await self.db.fetchone("SELECT status FROM browser_groups WHERE id = ?", (row["id"],))
            if current is None or current["status"] != "open":
                return False
            await self.db.execute(
                "UPDATE browser_groups SET status = ?, close_reason = ?, closed_at = ?, control_owner = 'agent', control_reason = '' WHERE id = ?",
                (status, reason, now_iso(), row["id"]),
            )
        self._notes.pop(row["id"], None)
        self._wake_waiters()
        await self.audit(row["id"], row["env"], by, "close", {"reason": reason})
        await self._publish("browser.closed", {"group_id": row["id"], "reason": reason, "by": by}, row)
        return True

    # -- profiles ---------------------------------------------------------------------------

    async def profiles(self) -> builtins.list[dict[str, Any]]:
        """The profiles the host asked for, with what each daemon says of them (size, running)."""
        rows = [dict(r) for r in await self.db.fetchall("SELECT * FROM browser_profiles ORDER BY last_used_at DESC")]
        live: dict[tuple[str, str], dict[str, Any]] = {}
        for env, link in self.links.items():
            if not link.available:
                continue
            with contextlib.suppress(BrowserError):
                listing = await self._call(env, "profile.list", {}, what="listing the profiles")
                for item in listing.get("profiles") or []:
                    live[(env, str(item.get("id")))] = item
        return [
            {
                "id": r["id"], "env": r["env"], "scope": r["scope"], "project_id": r["project_id"], "session_id": r["session_id"], "staff_id": r["staff_id"],
                "created_at": r["created_at"], "last_used_at": r["last_used_at"],
                "size_bytes": int((live.get((r["env"], r["id"])) or {}).get("size_bytes") or 0),
                "running": bool((live.get((r["env"], r["id"])) or {}).get("running")),
            }
            for r in rows
        ]

    async def profile_action(self, env: str, profile: str, action: str, *, actor: str = "operator") -> None:
        """Clear a profile's cookies and storage, or delete it; the daemon refuses while it runs."""
        if action not in ("clear", "delete") or not valid_id(profile):
            raise InvalidRequest("a profile is cleared or deleted by its id")
        await self._call(env, f"profile.{action}", {"profile": profile}, what=f"{'clearing' if action == 'clear' else 'deleting'} the profile")
        if action == "delete":
            await self.db.execute("DELETE FROM browser_profiles WHERE env = ? AND id = ?", (env, profile))
        await self.audit(f"profile:{profile}", env, actor, f"profile_{action}", {"profile": profile})

    # -- downloads and uploads --------------------------------------------------------------

    async def downloads(self, group: str) -> builtins.list[dict[str, Any]]:
        result = await self.call(group, "download.list", {"group_id": group}, what="listing the downloads")
        return [d for d in result.get("downloads") or [] if isinstance(d, dict)]

    async def read_download(self, group: str, download: dict[str, Any], *, limit: int) -> bytes:
        """The bytes of a finished download, read in chunks; refused past ``limit``."""
        size = int(download.get("size") or 0)
        if size > limit:
            raise InvalidRequest(f"{download.get('name')} is {size} bytes; at most {limit} are taken into a workspace")
        out = bytearray()
        while True:
            chunk = await self.call(group, "download.read", {"id": download["id"], "offset": len(out), "max": DOWNLOAD_CHUNK}, what="reading the download", timeout=30.0)
            out.extend(base64.b64decode(chunk.get("data_b64") or ""))
            if chunk.get("eof") or not chunk.get("data_b64") or len(out) > limit:
                break
        if len(out) > limit:
            raise InvalidRequest(f"{download.get('name')} grew past {limit} bytes while it was read")
        return bytes(out)

    async def upload(self, group: str, name: str, data: bytes) -> str:
        """Stream a file's bytes to the daemon's upload area; returns the upload id for ``page.act``."""
        upload_id = ""
        offset = 0
        while True:
            piece = data[offset : offset + UPLOAD_CHUNK]
            params: dict[str, Any] = {"group_id": group, "name": name, "offset": offset, "data_b64": base64.b64encode(piece).decode()}
            if upload_id:
                params["upload_id"] = upload_id
            result = await self.call(group, "upload.put", params, what="handing the file to the browser", timeout=30.0)
            upload_id = str(result.get("upload_id") or upload_id)
            offset += len(piece)
            if offset >= len(data):
                return upload_id

    # -- live views -------------------------------------------------------------------------

    async def attachable(self, group: str) -> dict[str, Any]:
        """The row of a group a live view may be handed a ticket for; a closed one is unknown."""
        row = await self._row(group)
        if row["status"] != "open":
            raise NotFound(f"browser {group} is not open")
        self._client(row["env"])
        return row

    async def attach(self, group: str, *, read_only: bool, label: str, via: str) -> Attachment:
        row = await self.attachable(group)
        client = self._client(row["env"])
        params = {"group_id": group, "client": {"kind": "viewer" if read_only else "human", "label": label[:256], "via": via[:256], "read_only": read_only}}
        try:
            result = await client.call("view.attach", params)
        except Unavailable as exc:
            raise EnvUnavailable(f"attaching: the {row['env']} browser service is not available: {exc.detail}", env=row["env"], reason=exc.reason) from None
        except wire.RpcError as exc:
            raise rpc_failure(exc, "attaching") from None
        return Attachment(group_id=group, env=row["env"], client_id=str(result.get("client_id") or ""), channel=client.channel(int(result["channel"])), client=client)

    # -- reconcile --------------------------------------------------------------------------

    async def _reconcile(self, link: Link) -> None:
        client = link.client
        if client is None:
            return
        instance = client.instance
        listing = await client.call("group.list", {})
        listed = {str(g.get("id")): g for g in listing.get("groups") or [] if isinstance(g, dict)}
        rows = [dict(r) for r in await self.db.fetchall("SELECT * FROM browser_groups WHERE env = ? AND status = 'open'", (link.env,))]
        for row in rows:
            info = listed.get(row["id"])
            if info is None:
                if row["daemon_instance"] and row["daemon_instance"] != instance:
                    await self._mark_closed(row, "lost", status="lost")
                else:
                    await self._mark_closed(row, "idle", status="closed")
                continue
            await self.db.execute(
                "UPDATE browser_groups SET daemon_instance = ?, browser_id = ?, tabs = ?, control_owner = ?, control_reason = ? WHERE id = ?",
                (instance, str(info.get("browser_id") or row["browser_id"]), _count(info.get("tabs")), self._control_owner(info), str((info.get("control") or {}).get("reason") or ""), row["id"]),
            )
        known = {r["id"] for r in await self.db.fetchall("SELECT id FROM browser_groups WHERE env = ? AND status = 'open'", (link.env,))}
        for gid, info in listed.items():
            if gid not in known:
                await self._adopt(link, info)
        await self._close_orphans(link)
        browsers = await client.call("browser.list", {})
        running = {str(b.get("id")): b for b in browsers.get("browsers") or [] if isinstance(b, dict)}
        for row in await self.db.fetchall("SELECT id FROM browsers WHERE env = ? AND status = 'running'", (link.env,)):
            if row["id"] not in running:
                await self.db.execute("UPDATE browsers SET status = 'exited', reason = CASE WHEN reason = '' THEN 'gone' ELSE reason END, exited_at = ? WHERE env = ? AND id = ?", (now_iso(), link.env, row["id"]))
        for bid, info in running.items():
            await self._browser_started(link, {"browser_id": bid, "profile": info.get("profile"), "pid": info.get("pid"), "started_at": info.get("started_at")})

    async def _adopt(self, link: Link, info: dict[str, Any]) -> None:
        """A group the daemon runs and no open row describes: this host went between opening it and
        writing it down, or its row was pruned. Its labels say whose it was; one without is closed."""
        gid = str(info.get("id") or "")
        owner = Owner.from_labels(info.get("labels") or {})
        if owner is None or not valid_id(gid):
            logger.warning("browser group %s in %s has no owner this host knows and is closed", gid, link.env)
            with contextlib.suppress(BrowserError):
                await self._call(link.env, "group.close", {"group_id": gid}, what="closing a group nobody owns")
            return
        now = now_iso()
        await self.db.execute(
            "INSERT INTO browser_groups(id, env, profile, browser_id, owner_kind, owner_id, project_id, session_id, staff_id, fresh, status, control_owner, tabs, daemon_instance, created_by, created_at, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 'system', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = 'open', close_reason = '', closed_at = NULL, env = excluded.env, browser_id = excluded.browser_id, daemon_instance = excluded.daemon_instance",
            (gid, link.env, str(info.get("profile") or ""), str(info.get("browser_id") or ""), owner.kind, owner.id, owner.project_id, owner.session_id, owner.staff_id,
             int(str(info.get("profile") or "") == EPHEMERAL), self._control_owner(info), _count(info.get("tabs")), link.instance, now, now),
        )
        await self.audit(gid, link.env, "system", "open", {"adopted": True})
        logger.warning("browser group %s in %s had no row and was adopted for %s %s", gid, link.env, owner.kind, owner.id)

    async def _close_orphans(self, link: Link) -> None:
        rows = await self.db.fetchall("SELECT * FROM browser_groups WHERE env = ? AND status = 'open'", (link.env,))
        for row in rows:
            if not await self.owners.exists(self.owner_of(dict(row))):
                logger.warning("browser %s outlived its %s %s and is closed", row["id"], row["owner_kind"], row["owner_id"])
                with contextlib.suppress(BrowserError):
                    await self.close_group(row["id"], actor="system", reason="owner_gone")

    # -- events -----------------------------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback) if callback in self._subscribers else None

    async def _on_notification(self, env: str, method: str, params: dict[str, Any]) -> None:
        link = self.links[env]
        if method == "events.resync":
            if link.available:
                await self._reconcile(link)
            return
        if method != "event":
            return
        seq = int(params.get("seq") or 0)
        if seq and seq <= link.event_seq:
            return
        link.event_seq = max(link.event_seq, seq)
        kind = str(params.get("type") or "")
        data = params.get("data") if isinstance(params.get("data"), dict) else {}
        assert isinstance(data, dict)
        await self._on_event(link, kind, data)
        for subscriber in list(self._subscribers):
            try:
                await subscriber(env, kind, data)
            except Exception:  # noqa: BLE001 — one subscriber's failure is not the others'
                logger.exception("a browser event subscriber failed on %s", kind)

    async def _group_row(self, data: dict[str, Any]) -> dict[str, Any] | None:
        gid = str(data.get("group_id") or "")
        if not gid:
            return None
        row = await self.db.fetchone("SELECT * FROM browser_groups WHERE id = ?", (gid,))
        return dict(row) if row is not None else None

    async def _on_event(self, link: Link, kind: str, data: dict[str, Any]) -> None:
        if kind == "browser.stats":
            self._on_stats(link, data)
            return
        if kind == "browser.started":
            await self._browser_started(link, data)
            return
        if kind == "browser.exited":
            await self._browser_exited(link, data)
            return
        row = await self._group_row(data)
        if row is None:
            return
        if kind == "group.closed":
            await self._mark_closed(row, "closed", status="closed")
        elif kind == "tab.updated":
            await self.db.execute("UPDATE browser_groups SET url = ?, title = ? WHERE id = ?", (str(data.get("url") or "")[:4000], str(data.get("title") or "")[:500], row["id"]))
        elif kind in ("tab.created", "tab.closed"):
            await self.db.execute("UPDATE browser_groups SET tabs = MAX(0, tabs + ?) WHERE id = ?", (1 if kind == "tab.created" else -1, row["id"]))
        elif kind == "control":
            await self._on_control(row, data)
        elif kind == "needs_you":
            await self._needs_you(row, str(data.get("reason") or "other"), str(data.get("what") or ""), str(data.get("url") or row["url"]))
        elif kind == "download.done":
            download = data.get("download") if isinstance(data.get("download"), dict) else {}
            assert isinstance(download, dict)
            await self.audit(row["id"], row["env"], "page", "download", {"id": download.get("id"), "name": download.get("name"), "size": download.get("size"), "sha256": download.get("sha256"), "state": download.get("state")})

    async def _on_control(self, row: dict[str, Any], data: dict[str, Any]) -> None:
        owner = str(data.get("owner") or "agent")
        if owner not in CONTROL_OWNERS:
            return
        before = row["control_owner"]
        await self.db.execute("UPDATE browser_groups SET control_owner = ?, control_reason = ? WHERE id = ?", (owner, str(data.get("reason") or "")[:300], row["id"]))
        if owner != "agent" or before == "agent" or row["status"] != "open":
            return
        note, by = self._notes.pop(row["id"], ("", "operator"))
        fresh = await self._row(row["id"])
        payload: dict[str, Any] = {"group_id": row["id"], "url": fresh["url"], "title": fresh["title"], "tabs": int(fresh["tabs"]), "by": by}
        if note:
            payload["note"] = note
        await self._publish("browser.returned", payload, fresh)
        if self.wake is not None:
            where = f"{fresh['title']} — {fresh['url']}" if fresh["title"] else fresh["url"] or "a blank page"
            text = f"[browser] The operator gave the browser back. Now on {where}." + (f" Their note: {note}" if note else "") + " Take a BrowserSnapshot before acting: the page may have changed."
            try:
                await self.wake(self.owner_of(fresh), text)
            except Exception:  # noqa: BLE001 — the browser is given back whether or not the owner could be woken
                logger.exception("could not tell the owner of browser %s that it was given back", row["id"])

    async def _browser_started(self, link: Link, data: dict[str, Any]) -> None:
        bid = str(data.get("browser_id") or "")
        if not bid:
            return
        await self.db.execute(
            "INSERT INTO browsers(id, env, profile, pid, status, chromium_version, daemon_instance, started_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?) "
            "ON CONFLICT(env, id) DO UPDATE SET status = 'running', pid = excluded.pid, daemon_instance = excluded.daemon_instance",
            (bid, link.env, str(data.get("profile") or ""), data.get("pid") if isinstance(data.get("pid"), int) else None, str(data.get("chromium_version") or ""), link.instance, iso(data.get("started_at")) or now_iso()),
        )

    async def _browser_exited(self, link: Link, data: dict[str, Any]) -> None:
        bid = str(data.get("browser_id") or "")
        reason = str(data.get("reason") or ("crashed" if data.get("crashed") else "closed"))
        await self.db.execute("UPDATE browsers SET status = 'exited', reason = ?, exited_at = ? WHERE env = ? AND id = ?", (reason, now_iso(), link.env, bid))
        groups = [str(g) for g in data.get("groups") or []]
        rows = await self.db.fetchall("SELECT * FROM browser_groups WHERE env = ? AND status = 'open' AND (browser_id = ? OR id IN (SELECT value FROM json_each(?)))", (link.env, bid, json.dumps(groups)))
        for row in rows:
            await self._mark_closed(dict(row), reason, status="closed")
        self._wake_waiters()

    def _on_stats(self, link: Link, data: dict[str, Any]) -> None:
        link.stats = data
        if not data.get("supported", True):
            return
        for sample in data.get("browsers") or []:
            if isinstance(sample, dict):
                self.costs.add(PROFILE, float(sample.get("rss_bytes") or 0), float(sample.get("cpu_percent") or 0))
                self._costs_dirty = True

    # -- load -------------------------------------------------------------------------------

    async def load(self, *, cap: int | None = None) -> dict[str, Any]:
        """What the running browsers cost now, and what the machine would carry at ``cap`` of them per
        environment. The memory is the daemon's private figure (anonymous and shared pages), never the
        sum of RSS, which counts Chromium's shared code once per process (measured: several times over)."""
        configured = self.config().running_cap
        target = cap if cap is not None else configured
        envs: builtins.list[dict[str, Any]] = []
        used_rss, used_cpu, running = 0, 0.0, 0
        machine: dict[str, Any] = {}
        for env, link in self.links.items():
            if not link.available:
                continue
            try:
                stats = await self._call(env, "browser.stats", {}, what="measuring the browsers")
            except BrowserError as exc:
                logger.info("browser stats of %s unavailable: %s", env, exc.message)
                continue
            browsers = [b for b in stats.get("browsers") or [] if isinstance(b, dict)]
            rss = sum(int(b.get("rss_bytes") or 0) for b in browsers)
            cpu = sum(float(b.get("cpu_percent") or 0) for b in browsers)
            daemon = stats.get("daemon") if isinstance(stats.get("daemon"), dict) else {}
            used_rss += rss + int(daemon.get("rss_bytes") or 0)
            used_cpu += cpu + float(daemon.get("cpu_percent") or 0)
            running += len(browsers)
            env_machine = stats.get("machine") or {}
            total, available = load_math.effective_memory(env_machine)
            envs.append({"env": env, "supported": bool(stats.get("supported", True)), "browsers": len(browsers), "rss_bytes": rss, "cpu_percent": round(cpu, 1), "mem_total_bytes": total, "mem_available_bytes": available})
            if env == "host" or not machine:
                machine = env_machine
        cost, basis = load_math.likely_cost(self.costs.profiles(), [PROFILE] * running)
        total, available = load_math.effective_memory(machine)
        return {
            "cap": configured,
            "running": running,
            "queued": self.queue(),
            "used": {"rss_bytes": used_rss, "cpu_percent": round(used_cpu, 1), "cpus": load_math.effective_cpus(machine), "mem_total_bytes": total, "mem_available_bytes": available},
            "likely": {**cost.view(), "basis": basis},
            "projection": load_math.project(cap=target, running=running, used_rss=used_rss, used_cpu=used_cpu, machine=machine, cost=cost),
            "envs": envs,
            "thresholds": {"warn": load_math.WARN_PERCENT, "bad": load_math.BAD_PERCENT},
        }


__all__ = ["Attachment", "Browsers", "GroupView", "Link", "Wake"]
