"""``Skill`` — load a skill body (and its file list) into the conversation."""

from __future__ import annotations

from typing import Any

from protocore.contracts.skills import SkillNotFoundError
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.services import locator
from daedalus.tools._common import error, ok


@tool(
    name="Skill",
    description=(
        "Load a skill by name. The skill's instructions are returned so you can follow "
        "them; supporting files are listed with their paths."
    ),
)
async def load_skill(context: ToolContext, skill: str) -> ToolResult:
    store = locator.get(context.session_id).extra.get("skill_store")
    if store is None:
        return error(context, "skills are not available in this session")
    try:
        bundle = await store.load(context.tenant_id, skill)
    except (SkillNotFoundError, KeyError):
        return error(context, f"no skill named {skill!r}")
    files = [f for f in await store.list_files(context.tenant_id, bundle.manifest.id) if f.path != "SKILL.md"]
    listing = _listing(files)
    text = f"# Skill: {bundle.manifest.name}\n{bundle.manifest.description}\n\n{bundle.body}"
    if listing:
        root = getattr(store, "root", None)
        where = f"{root}/{bundle.manifest.id}/" if root else f"skills/{bundle.manifest.id}/"
        text += f"\n\nFiles in this skill (under {where}; read or run them from there):\n{listing}"
    return ok(context, text, skill_id=bundle.manifest.id)


LISTING_MAX_FILES = 40
"""Past this many files the listing folds directories: a gallery skill indexes its own files in SKILL.md."""


def _listing(files: list[Any]) -> str:
    if len(files) <= LISTING_MAX_FILES:
        return "\n".join(f"- {f.path} ({f.size_bytes} bytes)" for f in files)
    top: dict[str, list[Any]] = {}
    for f in files:
        top.setdefault(f.path.split("/", 1)[0] if "/" in f.path else "", []).append(f)
    lines = []
    for name, group in top.items():
        if name:
            lines.append(f"- {name}/ ({len(group)} files, {sum(g.size_bytes for g in group)} bytes; see SKILL.md for the index)")
        else:
            lines.extend(f"- {f.path} ({f.size_bytes} bytes)" for f in group)
    return "\n".join(lines)


TOOLS = [load_skill]

__all__ = ["TOOLS"]
