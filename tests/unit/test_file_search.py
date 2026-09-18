"""Finding a file in a session's tree: by name, by content, and never outside the tree."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import api as api_module
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
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


async def _tree(manager: SessionManager, outside: Path) -> str:
    """A session whose workspace holds a small tree, a skipped folder, and two ways out of it."""
    state = await manager.create_session("files")
    root = state.workspace
    (root / "src" / "deep").mkdir(parents=True, exist_ok=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True, exist_ok=True)
    (root / "src" / "menu.json").write_text('{"needle": "in the haystack"}', encoding="utf-8")
    (root / "src" / "deep" / "report.md").write_text("nothing to see\n", encoding="utf-8")
    (root / "notes.txt").write_text("the needle again\n", encoding="utf-8")
    (root / "node_modules" / "left-pad" / "menu.json").write_text('{"needle": 1}', encoding="utf-8")
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "menu.json").write_text('{"needle": "secret"}', encoding="utf-8")
    (root / "escape.json").symlink_to(outside / "menu.json")
    (root / "out").symlink_to(outside, target_is_directory=True)
    return state.session.id


async def test_a_name_search_finds_files_and_folders_and_skips_the_noise(client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path) -> None:
    sid = await _tree(manager, tmp_path / "elsewhere")
    body = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "menu"}, headers=H)).json()
    assert [r["path"] for r in body["results"]] == ["src/menu.json"]
    assert body["results"][0]["kind"] == "file" and body["results"][0]["size"] > 0 and body["results"][0]["mtime"] > 0
    assert not body["truncated"]

    folders = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "deep"}, headers=H)).json()
    assert [(r["path"], r["kind"]) for r in folders["results"]] == [("src/deep", "dir")]

    globbed = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "*.md"}, headers=H)).json()
    assert [r["path"] for r in globbed["results"]] == ["src/deep/report.md"]

    empty = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "  "}, headers=H)).json()
    assert empty["results"] == [] and empty["engine"] == "none"


async def test_neither_search_ever_names_a_path_outside_the_tree(client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    sid = await _tree(manager, outside)
    names = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "escape"}, headers=H)).json()
    assert names["results"] == []  # a symlink to a file outside resolves out of the pane and is dropped
    assert (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "../elsewhere/menu.json"}, headers=H)).json()["results"] == []

    hits = (await client.get(f"/api/sessions/{sid}/files/grep", params={"q": "needle"}, headers=H)).json()
    assert {h["path"] for h in hits["hits"]} == {"src/menu.json", "notes.txt"}
    assert all(str(outside) not in h["path"] for h in hits["hits"])
    assert [h["line"] for h in hits["hits"] if h["path"] == "notes.txt"] == [1]
    assert "needle" in next(h["text"] for h in hits["hits"] if h["path"] == "notes.txt")


async def test_both_searches_stay_inside_their_bounds(client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = await manager.create_session("many")
    for i in range(40):
        (state.workspace / f"note-{i:03d}.txt").write_text("needle\n", encoding="utf-8")
    sid = state.session.id

    capped = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "note", "limit": 5}, headers=H)).json()
    assert len(capped["results"]) == 5 and capped["truncated"]
    grepped = (await client.get(f"/api/sessions/{sid}/files/grep", params={"q": "needle", "limit": 5}, headers=H)).json()
    assert len(grepped["hits"]) == 5 and grepped["truncated"]

    # A limit above the ceiling is clamped to it, never honoured as asked.
    monkeypatch.setattr(api_module, "FILE_SEARCH_MAX_RESULTS", 3)
    clamped = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "note", "limit": 10_000}, headers=H)).json()
    assert len(clamped["results"]) == 3 and clamped["truncated"]

    monkeypatch.setattr(api_module, "FILE_SEARCH_MAX_ENTRIES", 4)
    walked = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "note"}, headers=H)).json()
    assert walked["truncated"] and len(walked["results"]) <= 4


async def test_name_search_works_without_ripgrep_and_content_search_says_it_cannot(
    client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await _tree(manager, tmp_path / "elsewhere")
    monkeypatch.setattr(api_module.shutil, "which", lambda name: None)

    body = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "menu"}, headers=H)).json()
    assert body["engine"] == "walk" and [r["path"] for r in body["results"]] == ["src/menu.json"]
    assert (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "escape"}, headers=H)).json()["results"] == []

    response = await client.get(f"/api/sessions/{sid}/files/grep", params={"q": "needle"}, headers=H)
    assert response.status_code == 501 and "ripgrep" in response.json()["detail"]


async def test_a_search_on_a_session_that_is_not_there_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/sessions/nope/files/search", params={"q": "x"}, headers=H)).status_code == 404
    assert (await client.get("/api/sessions/nope/files/grep", params={"q": "x"}, headers=H)).status_code == 404
    assert (await client.get("/api/sessions/nope/files/search", params={"q": "x"})).status_code == 401


def _silent_rg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a ripgrep on the path that finds nothing and takes ten seconds not to say so.

    A real query that matches nothing writes no line at all, which is the case the budget has to
    bound: the reader is blocked in the pipe and no deadline in the loop around it is ever read.
    """
    folder = tmp_path / "slow-bin"
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "rg"
    script.write_text("#!/bin/sh\nsleep 10\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return script


def _running(marker: Path) -> bool:
    return subprocess.run(["pgrep", "-f", str(marker)], capture_output=True, check=False).returncode == 0


async def test_a_search_that_finds_nothing_still_answers_within_its_budget(
    client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = await manager.create_session("silent")
    sid = state.session.id
    marker = _silent_rg(tmp_path, monkeypatch)

    started = time.monotonic()
    body = (await client.get(f"/api/sessions/{sid}/files/grep", params={"q": "nothing-matches-this"}, headers=H)).json()
    elapsed = time.monotonic() - started
    assert body["hits"] == [] and body["truncated"]
    assert elapsed < api_module.FILE_GREP_BUDGET_SECONDS + 2.0, elapsed

    # And the walk is not left running behind the answer.
    for _ in range(50):
        if not _running(marker):
            break
        await asyncio.sleep(0.05)
    assert not _running(marker)


async def test_a_name_search_is_bounded_by_the_same_watchdog(
    client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = await manager.create_session("silent-names")
    sid = state.session.id
    marker = _silent_rg(tmp_path, monkeypatch)

    started = time.monotonic()
    body = (await client.get(f"/api/sessions/{sid}/files/search", params={"q": "nothing"}, headers=H)).json()
    elapsed = time.monotonic() - started
    assert body["results"] == [] and body["engine"] == "rg"
    assert elapsed < api_module.FILE_SEARCH_BUDGET_SECONDS + 2.0, elapsed
    for _ in range(50):
        if not _running(marker):
            break
        await asyncio.sleep(0.05)
    assert not _running(marker)


async def test_a_caller_that_goes_away_takes_its_ripgrep_with_it(
    client: httpx.AsyncClient, manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed tab is a cancelled request, and the walk it started must not outlive it."""
    state = await manager.create_session("abandoned")
    sid = state.session.id
    marker = _silent_rg(tmp_path, monkeypatch)
    monkeypatch.setattr(api_module, "FILE_GREP_BUDGET_SECONDS", 30.0)

    request = asyncio.ensure_future(client.get(f"/api/sessions/{sid}/files/grep", params={"q": "gone"}, headers=H))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if _running(marker):
            break
    assert _running(marker)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    for _ in range(60):
        if not _running(marker):
            break
        await asyncio.sleep(0.05)
    assert not _running(marker)


async def test_the_searches_have_a_pool_of_their_own(tmp_path: Path) -> None:
    """Their own threads, so a filter box over a large tree cannot take the process's with it."""
    pool = api_module._search_pool()
    assert pool is api_module._search_pool()
    assert pool._max_workers == api_module.FILE_SEARCH_WORKERS


async def test_a_path_with_a_colon_in_it_keeps_its_hits(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("colons")
    (state.workspace / "od:d name.txt").write_text("the needle\n", encoding="utf-8")
    body = (await client.get(f"/api/sessions/{state.session.id}/files/grep", params={"q": "needle"}, headers=H)).json()
    assert [(h["path"], h["line"]) for h in body["hits"]] == [("od:d name.txt", 1)]
