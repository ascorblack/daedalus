"""Every host module is on an execution path, and the proposal gates say why when one is not."""

from __future__ import annotations

import pathlib
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
        "daedalus/extensions/__init__.py": "EXTENSIONS = ('daedalus.extensions.loops',)\n",
        "daedalus/extensions/loops.py": "from ..host import runner\n",
        "daedalus/extensions/forgotten.py": "from ..host import runner\n",
        "daedalus/tools/__init__.py": "",
        "daedalus/tools/shell.py": "TOOLS = []\n",
    })
    assert reachability.unreachable_modules(root) == ["daedalus.extensions.forgotten", "daedalus.host.orphan", "daedalus.tools", "daedalus.tools.shell"]
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
    relevance_gate(root, ["daedalus/host/deleted.py"], None)  # a deleted module needs no path
    with pytest.raises(ProposalRefused, match="is the changed module itself"):
        relevance_gate(root, ["daedalus/host/model.py"], "daedalus.host.model")
    (root / "daedalus/host/runner.py").write_text("from daedalus.host.model import Invariant\n", encoding="utf-8")
    relevance_gate(root, ["daedalus/host/model.py"], "daedalus.host.runner:Runner")


def test_evidence_gate_wants_a_passing_receipt_that_names_the_change() -> None:
    changed = ["daedalus/host/boot_guard.py", "tests/unit/test_boot_guard.py"]
    with pytest.raises(ProposalRefused, match="no passing Verify receipt recorded"):
        evidence_gate(changed, [{"command": "uv run pytest tests -q", "passed": 0}], None)
    with pytest.raises(ProposalRefused, match="names the changed code"):
        evidence_gate(changed, [{"command": "uv run pytest tests -q", "passed": 1}], None)
    with pytest.raises(ProposalRefused, match="names the changed code"):
        evidence_gate(["daedalus/config.py"], [{"command": "cat config.toml", "passed": 1}], None)
    with pytest.raises(ProposalRefused, match="names the changed code"):
        evidence_gate(["daedalus/extensions/api.py"], [{"command": "curl -s http://127.0.0.1:8080/api/health", "passed": 1}], None)
    evidence_gate(changed, [{"command": "uv run pytest tests/unit/test_boot_guard.py -q", "passed": 1}], None)
    evidence_gate(changed, [{"command": "uv run python -c 'import daedalus.host.boot_guard'", "passed": 1}], "daedalus.app:serve")
    evidence_gate(changed, [{"command": "uv run python -m daedalus check", "passed": 1}], "daedalus.__main__:cmd_check")
    evidence_gate(["docs/DESIGN.md"], [{"command": "true", "passed": 1}], None)


def test_size_gate_asks_what_a_large_or_net_new_change_replaces() -> None:
    size_gate(120, {"daedalus/host/small.py": 40}, "A small thing.")
    with pytest.raises(ProposalRefused, match="does not say what it replaces"):
        size_gate(260, {}, "Adds a validation harness with four checks.")
    with pytest.raises(ProposalRefused, match="new modules over 150 lines"):
        size_gate(90, {"daedalus/host/store.py": 170}, "Adds a store.")
    size_gate(260, {"daedalus/host/store.py": 170}, "Replaces the ad-hoc dict in session_runner with a store; removes two helpers.")


def test_a_relative_dynamic_import_is_resolved_not_left_as_a_dot_name(tmp_path: Path) -> None:
    """The reported defect, in isolation: the module is live and loaded through
    ``importlib.import_module(".feature", package=__package__)``, and that relative import is the
    only mention of it anywhere in the tree. Before the fix the walk kept the literal ``.feature``,
    so ``daedalus.feature`` had no incoming edge and the gate refused a change that runs."""
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "import importlib\nresult = importlib.import_module('.feature', package=__package__).VALUE\n",
        "daedalus/feature.py": "VALUE = 7\n",
    })
    graph = reachability.import_graph(root)
    assert graph["daedalus.__main__"] >= {"daedalus.feature"}
    assert ".feature" not in graph["daedalus.__main__"]
    assert reachability.unreachable_modules(root) == []
    assert reachability.path_reaches(root, "daedalus.__main__", ["daedalus.feature"]) == (True, [])


def test_every_relative_form_python_resolves_is_resolved_the_same_way(tmp_path: Path) -> None:
    """``.mod``, ``..mod``, a positional ``package``, ``__name__`` inside a package ``__init__``, and a
    call that names no package: each resolves to the module the interpreter really loads. The runtime
    side of this comparison was checked separately against ``importlib`` itself."""
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "import importlib\nfrom daedalus.sub import loader, deep, loose\nfrom daedalus import pkg\nfrom daedalus.sub import positional\n",
        "daedalus/feature.py": "VALUE = 7\n",
        "daedalus/sub/__init__.py": "",
        "daedalus/sub/loader.py": "import importlib\nmod = importlib.import_module('.sibling', package=__package__)\n",
        "daedalus/sub/sibling.py": "OK = True\n",
        "daedalus/sub/deep.py": "import importlib\nmod = importlib.import_module('..feature', package=__package__)\n",
        "daedalus/sub/positional.py": "import importlib\nmod = importlib.import_module('.sibling', 'daedalus.sub')\n",
        "daedalus/sub/loose.py": "import importlib\nmod = importlib.import_module('.sibling')\n",
        "daedalus/pkg/__init__.py": "import importlib\nmod = importlib.import_module('.sibling', package=__name__)\n",
        "daedalus/pkg/sibling.py": "OK = True\n",
    })
    graph = reachability.import_graph(root)
    assert graph["daedalus.sub.loader"] >= {"daedalus.sub.sibling"}
    assert graph["daedalus.sub.positional"] >= {"daedalus.sub.sibling"}
    assert graph["daedalus.sub.loose"] >= {"daedalus.sub.sibling"}
    assert graph["daedalus.sub.deep"] >= {"daedalus.feature"}
    assert graph["daedalus.pkg"] >= {"daedalus.pkg.sibling"}
    assert reachability.unreachable_modules(root) == []


def test_a_package_the_source_does_not_name_is_not_guessed(tmp_path: Path) -> None:
    """A guard, not a regression proof: this behaviour is the same before and after the fix, and it is
    here so it cannot drift silently. ``package=where`` is a variable and ``package=elsewhere.__name__``
    is another module's attribute, so the walk keeps the literal and draws no edge. The cost is stated
    rather than hidden — a module reached only that way reads as unreached, and needs a path the walk
    can see. The gain is that no edge is invented either."""
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/app.py": "import importlib\nwhere = 'daedalus'\nmod = importlib.import_module('.feature', package=where)\n",
        "daedalus/feature.py": "VALUE = 7\n",
        "daedalus/other.py": "import importlib\nmod = importlib.import_module('.feature', package=elsewhere.__name__)\n",
    })
    graph = reachability.import_graph(root)
    assert ".feature" in graph["daedalus.app"]
    assert ".feature" in graph["daedalus.other"]
    assert reachability.path_reaches(root, "daedalus.app", ["daedalus.feature"]) == (False, ["daedalus.feature"])
    assert reachability.path_reaches(root, "daedalus.other", ["daedalus.other.feature"]) == (False, ["daedalus.other.feature"])


def test_a_relative_name_is_not_resolved_beyond_the_package_root(tmp_path: Path) -> None:
    """``..feature`` from a top-level module leaves the package, so there is no module it could name
    and the literal is kept — while the valid relative import in the same tree is still resolved."""
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.sub import deep\n",
        "daedalus/app.py": "import importlib\nmod = importlib.import_module('..feature', package=__package__)\n",
        "daedalus/sub/__init__.py": "",
        "daedalus/sub/deep.py": "import importlib\nmod = importlib.import_module('..feature', package=__package__)\n",
        "daedalus/feature.py": "VALUE = 7\n",
    })
    graph = reachability.import_graph(root)
    assert graph["daedalus.app"] >= {"..feature"}
    assert graph["daedalus.sub.deep"] >= {"daedalus.feature"}
    assert reachability.path_reaches(root, "daedalus.app", ["daedalus.feature"]) == (False, ["daedalus.feature"])
    assert reachability.unreachable_modules(root) == []


def test_a_change_to_an_entry_point_can_name_that_entry_point(tmp_path: Path) -> None:
    """The reported dead end: an entry point nothing imports had no name it could be proposed under.

    Naming the changed module itself is refused so an agent cannot park a dead module on its own
    name — but nothing imports a process entry point, so for ``daedalus/__main__.py`` every option
    was refused: itself ("is the changed module itself"), anything else ("does not reach"), and no
    path at all ("names no execution_path"). The change was unproposable. An entry point is the one
    module that is its own runner, so it may name itself; a module below it still may not.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.app import serve\n",
        "daedalus/app.py": "from daedalus.host import runner\n",
        "daedalus/host/__init__.py": "",
        "daedalus/host/runner.py": "",
    })
    relevance_gate(root, ["daedalus/__main__.py"], "daedalus.__main__")
    relevance_gate(root, ["daedalus/__main__.py", "tests/unit/test_main.py"], "daedalus.__main__:main")
    # Still refused one level down: a module that something else runs must name that runner.
    with pytest.raises(ProposalRefused, match="is the changed module itself"):
        relevance_gate(root, ["daedalus/host/runner.py"], "daedalus.host.runner")


def test_the_live_tree_has_no_entry_point_that_cannot_be_named() -> None:
    """The live form of the dead end above, and cheap enough to run every time. An entry point that
    something imports can be proposed by naming its importer; one that nothing imports has no other
    name, and naming itself must be accepted or the module is unproposable.

    Measured over the live tree with the gate itself: before the fix, 2 of 85 module files accepted
    no candidate path at all (``daedalus.__main__``, ``daedalus.bench.harbor``); after it, 0 do."""
    graph = reachability.import_graph(ROOT)
    orphans = 0
    for entry in reachability.ENTRY_POINTS:
        parts = entry.split(".")
        file = str(pathlib.PurePosixPath(*parts).with_suffix(".py"))
        if not (ROOT / file).is_file():
            file = str(pathlib.PurePosixPath(*parts) / "__init__.py")
        if not (ROOT / file).is_file():
            continue
        if any(entry in deps for deps in graph.values()):
            continue  # its importer can be named instead
        orphans += 1
        relevance_gate(ROOT, [file], entry)
    assert orphans >= 1, "no entry point is import-free any more: this test checked nothing"
    assert reachability.unreachable_modules(ROOT) == []
