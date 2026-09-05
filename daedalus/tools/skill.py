"""``Skill`` — load a skill body (and its file list) into the conversation."""

from __future__ import annotations

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
        bundle = await store.load(context.account_id or context.tenant_id, skill)
    except (SkillNotFoundError, KeyError):
        return error(context, f"no skill named {skill!r}")
    files = await store.list_files(context.account_id or context.tenant_id, bundle.manifest.id)
    listing = "\n".join(f"- {f.path} ({f.size_bytes} bytes)" for f in files if f.path != "SKILL.md")
    text = f"# Skill: {bundle.manifest.name}\n{bundle.manifest.description}\n\n{bundle.body}"
    if listing:
        text += f"\n\nFiles in this skill (under skills/{bundle.manifest.id}/):\n{listing}"
    return ok(context, text, skill_id=bundle.manifest.id)


TOOLS = [load_skill]

__all__ = ["TOOLS"]
