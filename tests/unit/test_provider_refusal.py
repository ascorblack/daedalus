"""A provider that refuses the request outright, and an operator who must be told.

The incident: a message in a session that had answered before. The endpoint answered every attempt
with ``HTTP 400 invalid_request_error`` — the client version was too old for the model — and the run
retried it twice as if it were a blip. With the retries spent it then completed "on its preserved
answer": the answer it found was the previous run's reply. No message was written, no error was
shown, and the run was recorded as completed.

The adapter now says which failures are permanent, the core does not retry them, and a run with
nothing to show for itself fails with the provider's words in the app, in Telegram and in the inbox.
A rate limit and a server error are still retried.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMProviderError, LLMRateLimitError, LLMRequest, LLMTimeoutError
from protocore.contracts.types import Message, MessageRole, TextBlock
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host import engine_factory
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint, classify_failure
from daedalus.stores.database import Database
from daedalus.transport.telegram.render import RunRenderer, RunView
from tests.support.waiting import until_await
from tests.unit.test_notification_router import Host, entries
from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished
from tests.unit.test_telegram_render import FakeOutbox

TOO_OLD = json.dumps({
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "Client 1.0 does not support this model; version 1.2 or newer is required.",
        "details": {"error_code": "client_version_too_old"},
    },
    "request_id": "req_1",
})


def _sse(text: str) -> str:
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _endpoint(replies: list[tuple[int, str]]) -> tuple[OpenAICompatibleProvider, list[httpx.Request]]:
    """The real adapter over a scripted server: each request takes the next reply, the last one repeats."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = replies[min(len(seen), len(replies)) - 1]
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    endpoint = ProviderEndpoint(id="claude", kind="openai", base_url="https://provider.test", api_key="k")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))), seen


def _instant_retries(monkeypatch: Any) -> None:
    """The retry budget as configured, without the backoff: the count is what the tests read."""
    original = engine_factory.runtime_constants

    def patched(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **kwargs).model_copy(update={
            "llm_transient_error_retry_backoff_base_seconds": 0.0,
            "llm_transient_error_retry_backoff_max_seconds": 0.0,
        })

    monkeypatch.setattr(engine_factory, "runtime_constants", patched)


def _request() -> LLMRequest:
    return LLMRequest(model="m", messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])])


async def _raised(status: int, body: str) -> BaseException:
    provider, _seen = _endpoint([(status, body)])
    try:
        async for _ in provider.stream_with_tools(_request()):
            pass
    except Exception as exc:  # noqa: BLE001 — the error is what is under test
        return exc
    raise AssertionError("the endpoint's refusal was not raised")


# -- the adapter's verdict ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (400, TOO_OLD, "model_not_found"),
        (400, json.dumps({"error": {"type": "invalid_request_error", "message": "messages: field required"}}), "format_error"),
        (401, json.dumps({"error": {"type": "authentication_error", "message": "invalid x-api-key"}}), "auth"),
        (403, json.dumps({"error": {"type": "permission_error", "message": "not allowed"}}), "auth"),
        (404, json.dumps({"error": {"message": "The model `nope` does not exist"}}), "model_not_found"),
        (422, "unprocessable", "format_error"),
    ],
)
async def test_a_refusal_is_raised_as_permanent_in_the_providers_words(status: int, body: str, reason: str) -> None:
    exc = await _raised(status, body)
    assert type(exc) is LLMProviderError
    verdict = exc.classified  # type: ignore[attr-defined]
    assert (verdict.reason, verdict.retryable) == (reason, False)
    if status == 400 and "details" in body:
        assert str(exc) == (
            "claude: HTTP 400: Client 1.0 does not support this model; version 1.2 or newer is required."
            " (client_version_too_old)"
        )


@pytest.mark.parametrize(
    ("status", "body", "kind", "reason"),
    [
        (429, json.dumps({"error": {"type": "rate_limit_error", "message": "slow down"}}), LLMRateLimitError, "rate_limit"),
        (500, "internal error", LLMProviderError, "server_error"),
        (502, "bad gateway", LLMProviderError, "server_error"),
        (503, json.dumps({"error": {"type": "api_error", "message": "unavailable"}}), LLMProviderError, "server_error"),
        (529, json.dumps({"error": {"type": "overloaded_error", "message": "Overloaded"}}), LLMProviderError, "overloaded"),
        (504, "gateway timeout", LLMTimeoutError, "timeout"),
    ],
)
async def test_a_blip_stays_retryable(status: int, body: str, kind: type, reason: str) -> None:
    exc = await _raised(status, body)
    assert type(exc) is kind
    verdict = exc.classified  # type: ignore[attr-defined]
    assert (verdict.reason, verdict.retryable) == (reason, True)


def test_an_error_inside_a_stream_is_judged_by_its_type_not_by_the_status_it_arrived_under() -> None:
    """A stream reports its error in a chunk after the 200, and the adapter raises it as a 500."""
    refused = classify_failure(500, json.dumps({"type": "invalid_request_error", "message": "bad tool schema"}))
    busy = classify_failure(500, json.dumps({"type": "overloaded_error", "message": "Overloaded"}))
    assert (refused.reason, refused.retryable) == ("format_error", False)
    assert (busy.reason, busy.retryable) == ("overloaded", True)


# -- the run ---------------------------------------------------------------------------------------


async def test_a_refused_run_is_not_retried_and_fails_in_the_providers_words(
    settings: Settings, db: Database, monkeypatch: Any
) -> None:
    """The live shape: the session has answered before, then the provider refuses the next message."""
    _instant_retries(monkeypatch)
    rung: dict[str, Any] = {"provider": ScriptedProvider([{"text": "Here is the complete summary you asked for."}])}
    manager = await _manager(settings, db, rung["provider"])
    manager.providers.rungs_for = lambda config, preset_id=None: [(rung["provider"], "scripted-model")]  # type: ignore[method-assign]
    host = Host(settings, db, manager)
    service = await host.install()
    events: list[TurnEvent] = []
    view = RunView(run_id="r1", model="m")
    renderer = RunRenderer(FakeOutbox(), view, edit_interval=0.0)

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)
        await renderer.handle(event)

    manager.add_sink(sink)
    try:
        state = await manager.create_session("release notes")
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "summarise the release notes")
        assert (await waiter)[0][2] == "completed"

        refusing, seen = _endpoint([(400, TOO_OLD)])
        rung["provider"] = refusing
        events.clear()
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "and what changed since yesterday?")
        assert (await waiter)[0][2] == "failed"

        assert len(seen) == 1, "a refusal is answered the same way every time; it is not asked again"
        reasons = [e.payload.get("reason") for e in events if e.type is EventType.STATE_CHANGED]
        assert "transient_llm_error_retry" not in reasons
        assert "stream_error_completed_answer_preserved" not in reasons
        errors = [e.payload for e in events if e.type is EventType.ERROR]
        assert errors and "does not support this model" in str(errors[-1]["message"])
        assert state.last_error_permanent is True
        assert state.outage_task is None, "a refusal is not an outage to wait out"

        # Telegram draws the line in the run it is showing.
        assert any("does not support this model" in line for line in view.narration), view.narration
        # The app reads the session as failed and carries the words.
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            detail = (await client.get(f"/api/sessions/{state.session.id}", headers={"X-Daedalus-Token": "tok"})).json()
        assert detail["status"] == "failed"
        assert "does not support this model" in detail["error"]

        # The inbox has the failed run, in the same words.
        async def failed_entry() -> bool:
            return any(entry["category"] == "run_failed" for entry in await entries(service))

        await until_await(failed_entry, "the failed-run notification")
        [entry] = [entry for entry in await entries(service) if entry["category"] == "run_failed"]
        assert "does not support this model" in entry["body"]
    finally:
        await host.close()


async def test_a_server_error_is_still_retried_and_the_run_completes(
    settings: Settings, db: Database, monkeypatch: Any
) -> None:
    _instant_retries(monkeypatch)
    provider, seen = _endpoint([(503, "upstream unavailable"), (200, _sse("recovered answer"))])
    manager = await _manager(settings, db, provider)  # type: ignore[arg-type]
    manager.providers.rungs_for = lambda config, preset_id=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    try:
        state = await manager.create_session("blip")
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "hello")
        assert (await waiter)[0][2] == "completed"
        assert len(seen) == 2
        reasons = [e.payload.get("reason") for e in events if e.type is EventType.STATE_CHANGED]
        assert reasons.count("transient_llm_error_retry") == 1
        assert state.last_error_permanent is False
    finally:
        await manager.close()
