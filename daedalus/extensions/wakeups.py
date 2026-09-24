"""A project orchestrator's wake-ups: the alarms it sets itself with ``WakeMe``, and the ones the
operator leaves it from the app.

A wake-up is a schedule of kind ``wake`` (:mod:`daedalus.extensions.scheduler`) aimed at the
orchestrator's session. When it fires the scheduler publishes ``schedule.fired`` on the project and
the orchestrator's wake queue delivers the note at once. A wake-up belongs to the project rather
than to one session: the listing finds it through any session that is or was the project's
orchestrator, and taking the office points every wake-up at the new holder.

What bounds them, and why: every wake-up is a turn of the strongest model the operator has, and a
fired wake-up is urgent, so the hourly cap on routine wake-ups does not hold it back. A cron that
fires every minute would therefore buy sixty turns an hour. So a recurring wake-up may not come
round more often than ``orchestrator.wake_cron_min_minutes``, and a project holds at most
``orchestrator.wakeups_max`` of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.stores.projects import Project

NOTE_MAX = 500
IN_MINUTES_MAX = 60 * 24 * 60
"""Sixty days: past that an alarm is a plan, and the brief or the board is the place for it."""
AT_MAX_DAYS = 366
CRON_CHECKS = 24
"""How many consecutive occurrences of a cron are measured to find its shortest gap."""
TICK_NOTE = "The scheduler looks every 30 seconds, so a wake-up can come up to half a minute late."

_OF_PROJECT = (
    "coalesce(json_extract(s.metadata, '$.orchestrator_of'), json_extract(s.metadata, '$.orchestrator_retired_of')) = ?"
)


class WakeupRefused(ValueError):
    """A wake-up that will not be set, said so that whoever asked can set a different one."""


def _zone(app: Application) -> tzinfo:
    """The operator's zone as their app last reported it: an ``at`` without an offset is their time."""
    manager = app.manager
    tz = manager.presence.locale()[1] if manager is not None else ""
    try:
        return ZoneInfo(tz) if tz else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def shortest_gap(cron: str, start: datetime) -> timedelta:
    """The shortest time between two of the next :data:`CRON_CHECKS` occurrences of ``cron``."""
    it = croniter(cron, start)
    previous = it.get_next(datetime)
    gap = timedelta(days=366)
    for _ in range(CRON_CHECKS):
        following = it.get_next(datetime)
        gap = min(gap, following - previous)
        previous = following
    return gap


def resolve_when(app: Application, *, at: str | None, in_minutes: int | None, cron: str | None, now: datetime | None = None) -> tuple[str | None, str | None]:
    """``(run_at, cron)`` for the scheduler from the three ways of saying when; exactly one is given."""
    given = [name for name, value in (("at", at), ("in_minutes", in_minutes), ("cron", cron)) if value not in (None, "")]
    if len(given) != 1:
        raise WakeupRefused("give exactly one of at, in_minutes or cron")
    moment = now or datetime.now(UTC)
    config = app.config.orchestrator
    if cron:
        cron = " ".join(cron.split())
        if not croniter.is_valid(cron):
            raise WakeupRefused(f"{cron!r} is not a cron expression (minute hour day month weekday, in UTC)")
        if shortest_gap(cron, moment) < timedelta(minutes=config.wake_cron_min_minutes):
            raise WakeupRefused(f"a recurring wake-up comes round at most every {config.wake_cron_min_minutes} minutes; every wake-up is a turn")
        return None, cron
    if in_minutes is not None:
        try:
            minutes = int(in_minutes)
        except (TypeError, ValueError) as exc:
            raise WakeupRefused("in_minutes is a whole number of minutes") from exc
        if not 1 <= minutes <= IN_MINUTES_MAX:
            raise WakeupRefused(f"in_minutes is between 1 and {IN_MINUTES_MAX}")
        return (moment + timedelta(minutes=minutes)).isoformat(), None
    try:
        when = datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WakeupRefused(f"{at!r} is not a moment; use ISO 8601, e.g. 2026-10-01T09:30 (the operator's time) or with an offset") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=_zone(app))
    when = when.astimezone(UTC)
    if when <= moment:
        raise WakeupRefused(f"{at} has already passed; give a moment in the future")
    if when - moment > timedelta(days=AT_MAX_DAYS):
        raise WakeupRefused(f"a wake-up is at most {AT_MAX_DAYS} days ahead")
    return when.isoformat(), None


async def count(app: Application, project_id: str) -> int:
    row = await app.db.fetchone(
        f"SELECT count(*) AS n FROM schedules sc JOIN sessions s ON s.id = sc.target_session WHERE sc.kind = 'wake' AND sc.enabled = 1 AND {_OF_PROJECT}",
        (project_id,),
    )
    return int(row["n"]) if row is not None else 0


async def set_wakeup(
    app: Application,
    project: Project,
    *,
    note: str,
    at: str | None = None,
    in_minutes: int | None = None,
    cron: str | None = None,
    by_session: str | None = None,
) -> dict[str, Any]:
    """Set a wake-up for the project's orchestrator. ``by_session`` is the orchestrator that set it
    itself; without it the operator did. Returns the wake-up as :func:`view` shows it."""
    scheduler = app.extensions.get("scheduler")
    if scheduler is None:
        raise WakeupRefused("the scheduler is not running on this installation")
    orchestrator = project.settings.orchestrator
    if not orchestrator.enabled or not orchestrator.session_id:
        raise WakeupRefused(f"{project.name} has no orchestrator to wake; switch it on first")
    text = " ".join((note or "").split())
    if not text:
        raise WakeupRefused("a wake-up needs a note: what to look at when it fires")
    if len(text) > NOTE_MAX:
        raise WakeupRefused(f"a note is at most {NOTE_MAX} characters")
    limit = app.config.orchestrator.wakeups_max
    if await count(app, project.id) >= limit:
        raise WakeupRefused(f"{project.name} already has {limit} wake-ups; cancel one first")
    run_at, recurring = resolve_when(app, at=at, in_minutes=in_minutes, cron=cron)
    try:
        created = await scheduler.create(
            name=text[:80],
            prompt=text,
            cron=recurring,
            run_at=run_at,
            kind="wake",
            target_session=orchestrator.session_id,
            created_by_session=by_session,
        )
    except ValueError as exc:
        raise WakeupRefused(str(exc)) from exc
    row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    return view(dict(row)) if row is not None else created


def view(row: dict[str, Any]) -> dict[str, Any]:
    """A wake-up as the app and the tools show it."""
    return {
        "id": row["id"],
        "note": row.get("prompt") or row.get("name") or "",
        "cron": row.get("cron"),
        "at": row.get("run_at"),
        "next_run_at": row.get("next_run_at"),
        "last_run_at": row.get("last_run_at"),
        "enabled": bool(row.get("enabled")),
        "set_by": "orchestrator" if row.get("created_by_session") else "operator",
        "created_at": row.get("created_at"),
    }


async def wakeups(app: Application, project_id: str, *, include_done: bool = False) -> list[dict[str, Any]]:
    """The project's wake-ups, soonest first. A one-off that has fired is done and left out unless asked."""
    rows = await app.db.fetchall(
        f"SELECT sc.* FROM schedules sc JOIN sessions s ON s.id = sc.target_session WHERE sc.kind = 'wake' AND {_OF_PROJECT}"
        + ("" if include_done else " AND sc.enabled = 1")
        + " ORDER BY sc.next_run_at IS NULL, sc.next_run_at, sc.created_at",
        (project_id,),
    )
    return [view(dict(r)) for r in rows]


async def cancel(app: Application, project_id: str, wakeup_id: str) -> bool:
    """Remove one of the project's wake-ups; False when the project has no such wake-up."""
    row = await app.db.fetchone(
        f"SELECT sc.id FROM schedules sc JOIN sessions s ON s.id = sc.target_session WHERE sc.id = ? AND sc.kind = 'wake' AND {_OF_PROJECT}",
        (wakeup_id, project_id),
    )
    if row is None:
        return False
    scheduler = app.extensions.get("scheduler")
    if scheduler is not None:
        return bool(await scheduler.delete(wakeup_id))
    await app.db.execute("DELETE FROM schedules WHERE id = ?", (wakeup_id,))
    return True


async def repoint(app: Application, project_id: str, session_id: str) -> None:
    """Aim every wake-up of the project at the session that now holds the office. Called when an
    orchestrator is switched on again after being off, and on a replacement, so the state block —
    which lists the wake-ups aimed at the session it is written for — shows them all."""
    await app.db.execute(
        f"UPDATE schedules SET target_session = ? WHERE kind = 'wake' AND target_session IN (SELECT s.id FROM sessions s WHERE {_OF_PROJECT})",
        (session_id, project_id),
    )


def describe(wakeup: dict[str, Any]) -> str:
    """One line for the orchestrator: id, when, note."""
    when = f"cron {wakeup['cron']} (UTC)" if wakeup.get("cron") else f"at {str(wakeup.get('next_run_at') or wakeup.get('at') or '')[:16].replace('T', ' ')} UTC"
    return f"[{wakeup['id']}] {when} — {wakeup['note']} (set by the {wakeup['set_by']})"


__all__ = ["NOTE_MAX", "TICK_NOTE", "WakeupRefused", "cancel", "count", "describe", "repoint", "resolve_when", "set_wakeup", "view", "wakeups"]
