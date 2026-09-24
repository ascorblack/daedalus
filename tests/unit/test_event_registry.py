"""The event registry is the contract between everything that publishes and everything that listens.

Three areas of the host publish on the bus and several subscribe; a type spelled differently at one
end is an event nobody ever receives, and nothing fails. These tests make the registry the only
place a type can come from.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import is_typeddict

from daedalus.host.events import GAP, REGISTRY, BusGap

PKG = Path(__file__).resolve().parents[2] / "daedalus"


def test_every_entry_is_a_typed_dict_and_every_name_is_dotted_lower_case() -> None:
    for name, spec in REGISTRY.items():
        assert is_typeddict(spec.payload), name
        assert name == name.lower() and " " not in name, name


def test_optional_keys_are_really_optional() -> None:
    """With postponed annotations a TypedDict reads ``NotRequired`` as a string and calls every key
    required; the module is written without them, and this is what notices if that changes."""
    assert "summary" in REGISTRY["run.finished"].payload.__optional_keys__  # type: ignore[attr-defined]
    assert "summary" not in REGISTRY["run.finished"].payload.__required_keys__  # type: ignore[attr-defined]
    assert BusGap.__required_keys__ == {"from", "to"}
    assert GAP not in REGISTRY, "the gap is yielded by the bus, never published"


def _published_in(source: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("publish", "publish_soon") or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((node.lineno, first.value))
    return found


def test_the_scan_sees_both_ways_of_publishing() -> None:
    """Before any producer exists the scan below finds nothing, which proves nothing; this does."""
    sample = 'await app.bus.publish("run.started", p)\nself.bus.publish_soon("permission.pendng", p)\ncapabilities.publish(caps, d)\n'
    assert sorted(_published_in(sample)) == [(1, "run.started"), (2, "permission.pendng")]


def test_every_type_published_anywhere_in_the_host_is_registered() -> None:
    unknown = [
        f"{path.relative_to(PKG.parent)}:{line}: {name}"
        for path in sorted(PKG.rglob("*.py"))
        for line, name in _published_in(path.read_text(encoding="utf-8"))
        if name not in REGISTRY
    ]
    assert not unknown, "publish only registered types (add the type and its payload to daedalus/host/events.py):\n" + "\n".join(unknown)
