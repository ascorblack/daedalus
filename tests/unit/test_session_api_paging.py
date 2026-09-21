"""The session endpoint as a client sees it: one page at a time, compressed, with the whole text a request away."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES
from starlette.requests import Request

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.host.transcript_view import TOOL_RESULT_PREVIEW_CHARS
from daedalus.stores.database import Database

H = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    made = SessionManager(settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def client(settings: Settings, db: Database, manager: SessionManager) -> Any:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


async def _session(manager: SessionManager, count: int) -> str:
    state = await manager.create_session("paged")
    await manager.sessions.append_transcript(
        state.session.id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"turn {i}")]) for i in range(count)]
    )
    return state.session.id


async def test_a_client_walks_back_through_the_session_a_page_at_a_time(client: httpx.AsyncClient, manager: SessionManager) -> None:
    sid = await _session(manager, 40)
    page = (await client.get(f"/api/sessions/{sid}", params={"tail": 10}, headers=H)).json()
    assert [m["text"] for m in page["messages"]] == [f"turn {i}" for i in range(30, 40)]
    assert page["has_older"] and page["first_seq"] == page["messages"][0]["seq"]
    older = (await client.get(f"/api/sessions/{sid}", params={"before": page["first_seq"], "tail": 10}, headers=H)).json()
    assert [m["text"] for m in older["messages"]] == [f"turn {i}" for i in range(20, 30)]
    oldest = (await client.get(f"/api/sessions/{sid}", params={"before": 1, "tail": 10}, headers=H)).json()
    assert oldest["messages"] == [] and not oldest["has_older"]
    whole = (await client.get(f"/api/sessions/{sid}", params={"tail": 999999}, headers=H)).json()
    assert len(whole["messages"]) == 40 and not whole["has_older"]  # a page is capped, not refused


async def test_a_listed_tool_result_is_a_preview_and_the_expand_endpoint_has_the_rest(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("tools")
    sid = state.session.id
    body = "output " * 2000
    await manager.sessions.append_transcript(
        sid,
        [
            Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="call-7", name="Exec", arguments_json='{"cmd": "ls"}')]),
            Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="call-7", content=body)]),
        ],
    )
    page = (await client.get(f"/api/sessions/{sid}", headers=H)).json()
    listed = [r for m in page["messages"] for r in m["tool_results"]]
    assert len(listed) == 1 and len(listed[0]["content"]) == TOOL_RESULT_PREVIEW_CHARS and listed[0]["length"] == len(body)
    full = (await client.get(f"/api/sessions/{sid}/tool-results/call-7", headers=H)).json()
    assert full["content"] == body and full["length"] == len(body)
    assert (await client.get(f"/api/sessions/{sid}/tool-results/nope", headers=H)).status_code == 404


async def test_the_page_goes_out_compressed_and_the_stream_does_not(client: httpx.AsyncClient, manager: SessionManager) -> None:
    sid = await _session(manager, 200)
    response = await client.get(f"/api/sessions/{sid}", headers={**H, "Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert len(response.content) > 1000  # httpx reports the decoded body; the wire form was smaller
    # The event stream is never compressed: a token must leave the process when it arrives and not
    # when a compression buffer fills. The middleware settles that by content type.
    assert "text/event-stream" in DEFAULT_EXCLUDED_CONTENT_TYPES


async def test_session_stream_pages_to_its_watermark_and_a_fresh_page_skips_old_events(
    settings: Settings,
    db: Database,
    manager: SessionManager,
) -> None:
    sid = await _session(manager, 1)
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    api = build_app(app, "tok")
    endpoint = next(route.endpoint for route in api.routes if getattr(route, "path", "") == "/api/sessions/{session_id}/stream")

    async def replay(session_id: str, *, after: int, through: int | None = None, limit: int = 500) -> dict[str, Any]:
        assert session_id == sid and limit == 1000
        watermark = 1002
        upper = min(watermark, through if through is not None else watermark, after + limit)
        events = [
            {
                "session_id": sid,
                "run_id": "run",
                "event_seq": seq,
                "history_revision": 0,
                "kind": "tool_started",
                "payload": {"seq": seq},
            }
            for seq in range(after + 1, upper + 1)
        ]
        return {"resync_required": False, "watermark": watermark, "history_revision": 0, "runtime_epoch": "", "events": events}

    manager.events.session_replay = replay  # type: ignore[method-assign]

    async def disconnected() -> dict[str, str]:
        return {"type": "http.disconnect"}

    resumed_request = Request(
        {"type": "http", "method": "GET", "path": f"/api/sessions/{sid}/stream", "query_string": b"after=1", "headers": []},
        disconnected,
    )
    resumed = await endpoint(session_id=sid, request=resumed_request, _={})
    resumed_frames = [chunk async for chunk in resumed.body_iterator]
    assert len(resumed_frames) == 1002  # hello plus every event from 2 through the fixed watermark
    assert resumed_frames[1].startswith("id: 2\n")
    assert resumed_frames[-1].startswith("id: 1002\n")

    fresh_request = Request(
        {"type": "http", "method": "GET", "path": f"/api/sessions/{sid}/stream", "query_string": b"", "headers": []},
        disconnected,
    )
    fresh = await endpoint(session_id=sid, request=fresh_request, _={})
    fresh_frames = [chunk async for chunk in fresh.body_iterator]
    assert len(fresh_frames) == 1 and fresh_frames[0].startswith("event: hello\n")
