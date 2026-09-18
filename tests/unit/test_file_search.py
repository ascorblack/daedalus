"""Finding a file in a session's tree: by name, by content, and never outside the tree."""

from __future__ import annotations

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
