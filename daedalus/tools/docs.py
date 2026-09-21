"""Read the public documentation shipped with this exact installation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for

MAX_RESULTS = 20
MAX_CHUNK = 16 * 1024
ROOT_PAGES = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "GOVERNANCE.md", "SECURITY.md")
EXCLUDED_DOCS = frozenset({"PLAN.md", "RESEARCH.md"})
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
WORD = re.compile(r"[\w-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Page:
    id: str
    source: str
    title: str
    text: str
    headings: tuple[tuple[str, int], ...]


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text.casefold()).strip("-")


def _root(context: ToolContext) -> Path | None:
    manager = services_for(context).extra.get("manager")
    root = getattr(getattr(manager, "settings", None), "bot_repo_dir", None)
    return Path(root).resolve() if root else None


def _pages(root: Path) -> tuple[list[Page], str]:
    paths = [root / name for name in ROOT_PAGES]
    docs = root / "docs"
    if docs.is_dir():
        paths.extend(path for path in docs.rglob("*.md") if path.name not in EXCLUDED_DOCS)
    pages: list[Page] = []
    digest = hashlib.sha256()
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.resolve().relative_to(root)
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        source = relative.as_posix()
        digest.update(source.encode())
        digest.update(b"\0")
        digest.update(text.encode())
        matches = tuple((_slug(match.group(2)), match.start()) for match in HEADING.finditer(text))
        first = next((match.group(2).strip() for match in HEADING.finditer(text)), path.stem)
        page_id = source.removesuffix(".md").casefold().replace("/", ":")
        pages.append(Page(page_id, source, first, text, matches))
    return pages, digest.hexdigest()


def _index(context: ToolContext) -> tuple[list[Page], str] | None:
    root = _root(context)
    return _pages(root) if root is not None and root.is_dir() else None


@tool(
    name="DocsSearch",
    description="Search the public documentation shipped with this installed Daedalus version. Returns page ids and headings; read a result with DocsRead.",
)
async def docs_search(context: ToolContext, query: str, limit: int = 10) -> ToolResult:
    index = _index(context)
    if index is None:
        return error(context, "installed documentation is unavailable")
    pages, version = index
    terms = [word.casefold() for word in WORD.findall(query)]
    if not terms:
        return error(context, "query has no searchable words")
    found: list[tuple[int, Page, str]] = []
    for page in pages:
        haystack = f"{page.title}\n{page.text}".casefold()
        if not all(term in haystack for term in terms):
            continue
        score = sum(page.title.casefold().count(term) * 8 + haystack.count(term) for term in terms)
        at = min((haystack.find(term) for term in terms if term in haystack), default=0)
        snippet = re.sub(r"\s+", " ", page.text[max(0, at - 80) : at + 240]).strip()
        found.append((score, page, snippet))
    found.sort(key=lambda item: (-item[0], item[1].id))
    capped = found[: max(1, min(int(limit), MAX_RESULTS))]
    body = "\n".join(f"- {page.id} — {page.title}: {snippet}" for _, page, snippet in capped)
    return ok(context, body or f"no installed documentation matches {query!r}", version=version, count=len(capped))


@tool(
    name="DocsRead",
    description="Read one page or heading from the installed documentation. Use the opaque next_cursor to continue a long section.",
)
async def docs_read(context: ToolContext, page: str, section: str = "", cursor: int = 0) -> ToolResult:
    index = _index(context)
    if index is None:
        return error(context, "installed documentation is unavailable")
    pages, version = index
    found = next((item for item in pages if item.id == page.casefold()), None)
    if found is None:
        return error(context, f"documentation page {page!r} is not in this installed version", version=version)
    start, end = 0, len(found.text)
    if section:
        wanted = _slug(section)
        positions = [position for slug, position in found.headings if slug == wanted]
        if not positions:
            return error(context, f"section {section!r} is not in {page!r}", version=version)
        start = positions[0]
        end = next((position for _, position in found.headings if position > start), len(found.text))
    offset = max(0, int(cursor))
    text = found.text[start + offset : min(end, start + offset + MAX_CHUNK)]
    next_cursor = offset + len(text) if start + offset + len(text) < end else None
    return ok(context, text, page=found.id, source=found.source, version=version, next_cursor=next_cursor)


TOOLS = [docs_search, docs_read]

__all__ = ["TOOLS"]
