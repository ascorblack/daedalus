"""Spend caps above the per-run one: the session's own, a provider's total, and the grand total."""

from __future__ import annotations

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openai_compat import UsageRecord
from daedalus.stores.database import Database


async def _record(manager: SessionManager, *, session_id: str, provider: str, usd: float | None, run_id: str = "r") -> None:
    await manager.usage.record(UsageRecord(provider_id=provider, model="m", purpose="stream", raw={}, normalized={"input_tokens": 10, "output_tokens": 1}, cost_usd=usd, duration_ms=1, run_id=run_id, session_id=session_id))


async def test_session_provider_and_total_caps(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    a = await manager.create_session("a")
    b = await manager.create_session("b")
    await _record(manager, session_id=a.session.id, provider="deepseek", usd=1.5)
    await _record(manager, session_id=b.session.id, provider="deepseek", usd=2.0)
    await _record(manager, session_id=b.session.id, provider="openrouter", usd=None)
    assert await manager.spend() == (3.5, 1) and await manager.spend(session_id=a.session.id) == (1.5, 0)
    # no caps: nothing breaches
    assert await manager.cap_breach(a, "deepseek") is None
    # the session's own cap
    assert await manager.set_session_cap(a.session.id, 1.0) == 1.0
    refreshed = await manager.sessions.get(a.session.id, "daedalus")
    assert refreshed.metadata["usd_cap"] == 1.0
    kind, note = await manager.cap_breach(a, "deepseek")
    assert kind == "session_cap" and "$1.50" in note
    assert await manager.cap_breach(b, "deepseek") is None
    with pytest.raises(ValueError):
        await manager.set_session_cap(a.session.id, -1)
    await manager.set_session_cap(a.session.id, None)
    assert await manager.cap_breach(a, "deepseek") is None and "usd_cap" not in a.metadata
    # a provider cap counts every session of that provider only
    manager.config.limits.usd_total_per_provider = {"deepseek": 3.0}
    assert (await manager.cap_breach(b, "deepseek"))[0] == "provider_cap"
    assert await manager.cap_breach(b, "openrouter") is None
    # the window resets from limits.total_since
    manager.config.limits.total_since = "2999-01-01T00:00:00+00:00"
    assert await manager.cap_breach(b, "deepseek") is None
    manager.config.limits.total_since = ""
    # the grand total
    manager.config.limits.usd_total_per_provider = {}
    manager.config.limits.usd_total = 3.0
    assert (await manager.cap_breach(b, None))[0] == "total_cap"
    manager.config.limits.usd_total = 10.0
    assert await manager.cap_breach(b, None) is None
    # submit refuses a session at its cap before spending anything
    await manager.set_session_cap(b.session.id, 0.5)
    with pytest.raises(RuntimeError, match="session cap reached"):
        await manager.submit(b.session.id, "hello")
    await manager.close()
