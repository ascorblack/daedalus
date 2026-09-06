"""Inbox entries, heartbeat gating, schedule kinds (reminder, lazy note, agent) and failure accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.heartbeat import Heartbeat, in_active_hours
from daedalus.extensions.inbox import Inbox, format_entries
from daedalus.extensions.scheduler import Scheduler
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        self.sent.append(text)
        return 1


class FakeFront:
    def __init__(self) -> None:
        self.outbox = FakeOutbox()
        self.notified: list[str] = []

    async def outbox_for_session(self, session_id: str) -> FakeOutbox:
        return self.outbox

    async def notify(self, text: str, *, markdown: bool = True) -> None:
        self.notified.append(text)


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=FakeFront(), extensions={})

    async def save_config(config: RuntimeConfig) -> None:
        app.config = config

    app.save_config = save_config
    app.extensions["inbox"] = Inbox(app)  # type: ignore[arg-type]
    yield app
    await manager.close()


async def test_inbox_post_list_and_read(app: Any) -> None:
    inbox: Inbox = app.extensions["inbox"]
    first = await inbox.post("heartbeat", "Heartbeat: quiet", "checked mail", severity="bogus")
    await inbox.post("schedule_failed", "Task failed", "boom", severity="error")
    assert await inbox.unread_count() == 2
    entries = await inbox.list(unread_only=True)
    assert [e["title"] for e in entries] == ["Task failed", "Heartbeat: quiet"]
    assert entries[1]["severity"] == "info"  # an unknown severity falls back
    assert await inbox.mark_read([first]) == 1
    assert await inbox.unread_count() == 1
    assert await inbox.mark_read() == 1
    text = format_entries(await inbox.list())
    assert "❌" in text and "Task failed" in text


def test_active_hours_windows() -> None:
    at = lambda h, m=0: datetime(2026, 9, 6, h, m, tzinfo=UTC)  # noqa: E731
    assert in_active_hours(at(9), "08:00-23:00") and not in_active_hours(at(7, 59), "08:00-23:00")
    assert in_active_hours(at(23, 30), "22:00-06:00") and in_active_hours(at(2), "22:00-06:00") and not in_active_hours(at(12), "22:00-06:00")
    assert in_active_hours(at(3), "garbage")


async def test_heartbeat_is_off_without_text_and_respects_interval(app: Any, tmp_path: Any) -> None:
    hb = Heartbeat(app)
    app.config.heartbeat.enabled = True
    app.config.heartbeat.active_hours = "00:00-00:00"
    assert not hb.due()  # empty file
    hb.write("check the mail")
    assert hb.due()
    hb.last_run = datetime.now(UTC) - timedelta(minutes=5)
    assert not hb.due()
    hb.last_run = datetime.now(UTC) - timedelta(minutes=app.config.heartbeat.interval_minutes + 1)
    assert hb.due()
    hb.runs_today = (datetime.now(UTC).strftime("%Y-%m-%d"), app.config.heartbeat.max_runs_per_day)
    assert not hb.due()
    app.config.heartbeat.enabled = False
    assert not hb.due()


async def test_message_reminder_is_delivered_without_a_model_call(app: Any) -> None:
    scheduler = Scheduler(app)
    created = await scheduler.create(name="pills", prompt="take the pills", cron=None, run_at="2026-01-01T00:00:00Z", kind="message", created_by_session="s1")
    assert created["kind"] == "message"
    row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    await scheduler.fire(dict(row))
    assert app.front.outbox.sent and "take the pills" in app.front.outbox.sent[0]
    inbox: Inbox = app.extensions["inbox"]
    assert [e["kind"] for e in await inbox.list()] == ["reminder"]
    row = await app.db.fetchone("SELECT enabled, next_run_at FROM schedules WHERE id = ?", (created["id"],))
    assert row["enabled"] == 0 and row["next_run_at"] is None  # a one-shot is done


async def test_lazy_note_rides_with_the_next_message_and_is_promoted_when_ignored(app: Any) -> None:
    scheduler = Scheduler(app)
    created = await scheduler.create(name="ask", prompt="ask about the invoice", cron=None, run_at="2026-01-01T00:00:00Z", kind="lazy", created_by_session="s1")
    row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    await scheduler.fire(dict(row))
    assert app.front.outbox.sent == []
    decorated = await scheduler.decorate_prompt("s1", "hi again")
    assert decorated.startswith("[Reminder fired") and decorated.endswith("\n\nhi again") and "ask about the invoice" in decorated
    assert "ask about the invoice" in await scheduler.decorate_prompt("s1", "x")  # not yet committed: a refused start loses nothing
    await scheduler.on_run_started("s1", "run-1")
    assert await scheduler.decorate_prompt("s1", "x") == "x"  # delivered once the run exists
    # a second note that nobody reads for a day is promoted
    await app.db.execute(
        "INSERT INTO lazy_notes(schedule_id, session_id, text, fired_at) VALUES (?, ?, ?, ?)",
        (created["id"], "s1", "stale note", (datetime.now(UTC) - timedelta(hours=30)).isoformat()),
    )
    promoted: list[tuple[str, str]] = []

    async def fake_run(title: str, prompt: str, workspace: Any, metadata: dict[str, Any], **_: Any) -> Any:
        promoted.append((title, prompt))
        return SimpleNamespace(session=SimpleNamespace(id="new"), run_id="r")

    scheduler.run_task_session = fake_run  # type: ignore[method-assign]
    await scheduler._promote_lazy_notes(datetime.now(UTC))
    assert promoted and "stale note" in promoted[0][1]
    row = await app.db.fetchone("SELECT promoted_at FROM lazy_notes WHERE text = 'stale note'")
    assert row["promoted_at"] is not None


async def test_failed_recurring_task_is_switched_off_after_max_failures(app: Any) -> None:
    scheduler = Scheduler(app)
    app.config.scheduler.max_failures = 2
    created = await scheduler.create(name="nightly", prompt="do it", cron="0 3 * * *", run_at=None, kind="agent")
    for n in (1, 2):
        scheduler._active[created["id"]] = "sess"
        await scheduler.on_run_finished("sess", f"run{n}", "failed")
        row = await app.db.fetchone("SELECT failure_count, enabled FROM schedules WHERE id = ?", (created["id"],))
        assert row["failure_count"] == n
    assert row["enabled"] == 0
    inbox: Inbox = app.extensions["inbox"]
    titles = [e["title"] for e in await inbox.list()]
    assert any("switched off" in t for t in titles)
    await scheduler.set_enabled(created["id"], True)
    row = await app.db.fetchone("SELECT failure_count, enabled FROM schedules WHERE id = ?", (created["id"],))
    assert row["enabled"] == 1 and row["failure_count"] == 0
