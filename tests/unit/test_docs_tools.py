from __future__ import annotations

from types import SimpleNamespace

import pytest

from daedalus.tools.docs import MAX_CHUNK, _pages, docs_read, docs_search


def test_manifest_only_contains_the_public_documentation(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("# Hello\nInstalled guide", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text("# Guide\nРусский поиск", encoding="utf-8")
    (tmp_path / "docs" / "PLAN.md").write_text("# Private plan", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("secret", encoding="utf-8")
    pages, version = _pages(tmp_path)
    assert [page.id for page in pages] == ["readme", "docs:guide"]
    assert len(version) == 64


@pytest.mark.asyncio
async def test_search_and_read_are_bounded(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n## Поиск\n" + "данные " * 4000, encoding="utf-8")
    context = SimpleNamespace(metadata={})
    manager = SimpleNamespace(settings=SimpleNamespace(bot_repo_dir=tmp_path))
    services = SimpleNamespace(extra={"manager": manager}, max_tool_output_chars=60_000)
    monkeypatch.setattr("daedalus.tools.docs.services_for", lambda _: services)
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: services)
    searched = await docs_search().invoke(context, {"query": "поиск", "limit": 99})
    assert "docs:guide" in searched.content
    read = await docs_read().invoke(context, {"page": "docs:guide", "section": "Поиск", "cursor": 0})
    assert len(read.content.encode("utf-8")) <= MAX_CHUNK
    assert read.metadata["next_cursor"] is not None


@pytest.mark.asyncio
async def test_read_cursor_pages_multibyte_text_without_splitting_it(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir()
    source = "# Guide\n" + "данные 🧪 " * 4000
    (tmp_path / "docs" / "guide.md").write_text(source, encoding="utf-8")
    context = SimpleNamespace(metadata={})
    manager = SimpleNamespace(settings=SimpleNamespace(bot_repo_dir=tmp_path))
    services = SimpleNamespace(extra={"manager": manager}, max_tool_output_chars=60_000)
    monkeypatch.setattr("daedalus.tools.docs.services_for", lambda _: services)
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: services)

    chunks: list[str] = []
    cursor = 0
    while True:
        read = await docs_read().invoke(context, {"page": "docs:guide", "cursor": cursor})
        assert len(read.content.encode("utf-8")) <= MAX_CHUNK
        chunks.append(read.content)
        next_cursor = read.metadata["next_cursor"]
        if next_cursor is None:
            break
        assert next_cursor > cursor
        cursor = next_cursor
    assert "".join(chunks) == source
