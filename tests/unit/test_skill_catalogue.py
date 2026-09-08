"""Every skill directory is readable by the store and the catalogue fits the prompt budget with descriptions."""

from __future__ import annotations

from pathlib import Path

import pytest
from protocore.runtime.skill_index import render_skills_catalog

from daedalus.host.engine_factory import runtime_constants
from daedalus.host.skills import DirectorySkillStore

SKILLS = Path(__file__).resolve().parents[2] / "skills"


async def test_every_skill_has_a_name_matching_its_directory_and_a_one_line_description() -> None:
    store = DirectorySkillStore(SKILLS)
    entries = await store.list("t")
    dirs = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())
    assert sorted(e.id for e in entries) == dirs
    for entry in entries:
        assert entry.name == entry.id, entry
        assert 40 <= len(entry.description) <= 320, (entry.id, len(entry.description))
        assert "\n" not in entry.description
        bundle = await store.load("t", entry.id)
        assert bundle.body.strip(), entry.id


async def test_catalogue_keeps_descriptions_at_the_smallest_configured_window() -> None:
    from daedalus.config import RuntimeConfig

    store = DirectorySkillStore(SKILLS)
    entries = await store.list("t")
    rc = runtime_constants(RuntimeConfig(), context_window=128_000, max_output_tokens=8_000, thinking=False)
    budget = int(128_000 * rc.skill_index_budget_ratio)

    async def count(text: str) -> int:
        return len(text) // 3  # pessimistic: three characters per token

    block = await render_skills_catalog(entries, token_counter=count, budget_tokens=budget)
    assert all(e.description[:30] in block for e in entries), "the catalogue degraded to bare names"


@pytest.mark.parametrize("skill", ["webapp-testing", "web-design-reviewer", "canvas-design"])
async def test_browser_and_canvas_skills_name_the_installed_tooling(skill: str) -> None:
    body = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "playwright" in body.lower() or "PIL" in body
