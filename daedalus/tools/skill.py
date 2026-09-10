"""``Skill`` — load a skill body (and its file list) into the conversation."""

from __future__ import annotations

import re
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
        "them; supporting files are listed with their paths. A skill that declares parameters is a recipe: "
        "pass them in args ({name: value}) and its {{name}} placeholders are filled."
    ),
)
async def load_skill(context: ToolContext, skill: str, args: dict[str, Any] | None = None) -> ToolResult:
    store = locator.get(context.session_id).extra.get("skill_store")
    if store is None:
        return error(context, "skills are not available in this session")
    try:
        bundle = await store.load(context.tenant_id, skill)
    except (SkillNotFoundError, KeyError):
        return error(context, f"no skill named {skill!r}")
    params = store.params_of(bundle.manifest.id) if hasattr(store, "params_of") else []
    body = bundle.body
    if params:
        given = {k: str(v) for k, v in (args or {}).items()}
        missing = [p for p in params if p not in given]
        for name, value in given.items():
            body = body.replace("{{" + name + "}}", value)
        if missing:
            body = f"[This skill takes parameters: {', '.join(params)}. Not given: {', '.join(missing)} — their {{{{placeholders}}}} are left as they are.]\n\n" + body
    files = [f for f in await store.list_files(context.tenant_id, bundle.manifest.id) if f.path != "SKILL.md"]
    listing = _listing(files)
    text = f"# Skill: {bundle.manifest.name}\n{bundle.manifest.description}\n\n{body}"
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


@tool(
    name="SkillDraft",
    description=(
        "Save a skill you distilled from this session as a draft: name (lowercase, hyphens), a one-line description "
        "that says when to use it, and the body (the procedure, the pitfalls, the commands that worked). The draft is "
        "written under the state directory; to make it a real skill, copy the directory into skills/ in a worktree of "
        "the host repository and open a pull request with SelfPropose. A skill earns its place with a case where it "
        "made the difference, so say which task produced it."
    ),
)
async def skill_draft(context: ToolContext, name: str, description: str, body: str, params: str | None = None) -> ToolResult:
    services = locator.get(context.session_id)
    manager = services.extra.get("manager")
    if manager is None:
        return error(context, "skill drafts are not available in this session")
    slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    if not slug or len(slug) > 48:
        return error(context, "name must be 1–48 characters of lowercase letters, digits and hyphens")
    description = " ".join(description.split())
    if not (20 <= len(description) <= 320):
        return error(context, "description must be one line of 20–320 characters saying when to use the skill")
    if len(body.strip()) < 80:
        return error(context, "the body is too short to be a skill; write the procedure, not a note")
    directory = manager.settings.state_dir / "skill-drafts" / slug
    directory.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {slug}\ndescription: {description}\n" + (f"params: {params.strip()}\n" if params and params.strip() else "") + "---\n"
    (directory / "SKILL.md").write_text(front + body.strip() + "\n", encoding="utf-8")
    return ok(context, f"draft saved at {directory}/SKILL.md (session {context.session_id}). To ship it: SelfWorkspace('bot', 'skill-{slug}'), copy the directory into skills/{slug}/ there, Verify that Skill(skill=\"{slug}\") loads in a check, commit, SelfPropose with execution_path='daedalus.host.skills'.", path=str(directory))


TOOLS = [load_skill, skill_draft]

__all__ = ["TOOLS"]
