"""Every host module is on an execution path, and the proposal gates say why when one is not."""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.extensions.selfdev import ProposalRefused, evidence_gate, relevance_gate, size_gate
from daedalus.host import reachability

ROOT = Path(__file__).resolve().parents[2]


def test_the_live_tree_has_no_module_nothing_reaches() -> None:
    assert reachability.unreachable_modules(ROOT) == []


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def test_static_walk_follows_imports_discovery_and_relative_imports(tmp_path: Path) -> None:
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.app import serve\n",
        "daedalus/app.py": "import importlib\nfrom .host import runner\nimportlib.import_module('daedalus.extensions')\n",
        "daedalus/host/__init__.py": "",
        "daedalus/host/runner.py": "from daedalus.host.skills import Store\n",
        "daedalus/host/skills.py": "class Store: ...\n",
        "daedalus/host/orphan.py": "x = 1\n",
        "daedalus/extensions/__init__.py": "",
        "daedalus/extensions/loops.py": "from ..host import runner\n",
        "daedalus/tools/__init__.py": "",
        "daedalus/tools/shell.py": "TOOLS = []\n",
    })
    assert reachability.unreachable_modules(root) == ["daedalus.host.orphan", "daedalus.tools", "daedalus.tools.shell"]
    ok, missing = reachability.path_reaches(root, "daedalus.extensions.loops:install", ["daedalus.host.skills"])
    assert ok and missing == []
    ok, missing = reachability.path_reaches(root, "daedalus.app", ["daedalus.host.orphan"])
    assert not ok and missing == ["daedalus.host.orphan"]
    assert reachability.path_reaches(root, "daedalus.nowhere", ["daedalus.app"]) == (False, ["no module 'daedalus.nowhere'"])


def test_relevance_gate_refuses_an_unwired_module_and_a_wrong_path(tmp_path: Path) -> None:
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "import daedalus.app\n",
        "daedalus/app.py": "from daedalus.host import runner\n",
        "daedalus/host/__init__.py": "",
        "daedalus/host/runner.py": "",
        "daedalus/host/model.py": "class Invariant: ...\n",
    })
    with pytest.raises(ProposalRefused, match="names no execution_path"):
        relevance_gate(root, ["daedalus/host/model.py", "tests/unit/test_model.py"], None)
    with pytest.raises(ProposalRefused, match="does not reach: daedalus.host.model"):
        relevance_gate(root, ["daedalus/host/model.py"], "daedalus.host.runner")
    relevance_gate(root, ["tests/unit/test_model.py", "skills/x/SKILL.md", "miniapp/src/App.tsx"], None)  # no host module: no path needed
    (root / "daedalus/host/runner.py").write_text("from daedalus.host.model import Invariant\n", encoding="utf-8")
    relevance_gate(root, ["daedalus/host/model.py"], "daedalus.host.runner:Runner")


def test_evidence_gate_wants_a_passing_receipt_that_names_the_change() -> None:
    changed = ["daedalus/host/boot_guard.py", "tests/unit/test_boot_guard.py"]
    with pytest.raises(ProposalRefused, match="no passing Verify receipt recorded"):
        evidence_gate(changed, [{"command": "uv run pytest tests -q", "passed": 0}], None)
    with pytest.raises(ProposalRefused, match="mentions the changed code"):
        evidence_gate(changed, [{"command": "uv run pytest tests -q", "passed": 1}], None)
    evidence_gate(changed, [{"command": "uv run pytest tests/unit/test_boot_guard.py -q", "passed": 1}], None)
    evidence_gate(changed, [{"command": "uv run python -c 'import daedalus.host.boot_guard'", "passed": 1}], "daedalus.app:serve")
    evidence_gate(["docs/DESIGN.md"], [{"command": "true", "passed": 1}], None)


def test_size_gate_asks_what_a_large_or_net_new_change_replaces() -> None:
    size_gate(120, {"daedalus/host/small.py": 40}, "A small thing.")
    with pytest.raises(ProposalRefused, match="does not say what it replaces"):
        size_gate(260, {}, "Adds a validation harness with four checks.")
    with pytest.raises(ProposalRefused, match="new modules over 150 lines"):
        size_gate(90, {"daedalus/host/store.py": 170}, "Adds a store.")
    size_gate(260, {"daedalus/host/store.py": 170}, "Replaces the ad-hoc dict in session_runner with a store; removes two helpers.")
