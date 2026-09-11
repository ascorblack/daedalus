"""Every host module is on an execution path, and the proposal gates say why when one is not."""

from __future__ import annotations

import importlib
import pathlib
from pathlib import Path

import pytest

from daedalus.extensions.selfdev import (
    SELF_NAMABLE,
    ProposalRefused,
    evidence_gate,
    relevance_gate,
    size_gate,
)
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
    call that names no package: each resolves to the module the interpreter really loads.

    The runtime side is not taken on trust any more: ``test_the_walk_agrees_with_importlib`` compares
    every form here against ``importlib.util.resolve_name``, the interpreter's own resolver, and
    against the interpreter's refusal for the form that names no package."""
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "import importlib\nfrom daedalus.sub import loader, deep, loose\nfrom daedalus import pkg\nfrom daedalus.sub import positional\n",
        "daedalus/feature.py": "VALUE = 7\n",
        "daedalus/sub/__init__.py": "",
        "daedalus/sub/loader.py": "import importlib\nmod = importlib.import_module('.sibling', package=__package__)\n",
        "daedalus/sub/sibling.py": "OK = True\n",
        "daedalus/sub/deep.py": "import importlib\nmod = importlib.import_module('..feature', package=__package__)\n",
        "daedalus/sub/positional.py": "import importlib\nmod = importlib.import_module('.sibling', 'daedalus.sub')\n",
        # No package argument: the interpreter refuses this call, so the walk must refuse to
        # invent an edge for it. Kept in the fixture as the negative case (see the test below).
        "daedalus/sub/loose.py": "import importlib\nmod = importlib.import_module('.sibling')\n",
        "daedalus/pkg/__init__.py": "import importlib\nmod = importlib.import_module('.sibling', package=__name__)\n",
        "daedalus/pkg/sibling.py": "OK = True\n",
    })
    graph = reachability.import_graph(root)
    assert graph["daedalus.sub.loader"] >= {"daedalus.sub.sibling"}
    assert graph["daedalus.sub.positional"] >= {"daedalus.sub.sibling"}
    assert ".sibling" in graph["daedalus.sub.loose"]  # no package named: literal kept, no edge drawn
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
    """The live form of the dead end above. It asserts two things and says which is which.

    First, every module file in the tree is reachable from the process entry points, so the entry
    point is the only kind of module whose own name may be the only candidate left. Second, for each
    entry point that nothing imports, the gate accepts naming it — the case that had no answer at
    all before the change. That is behaviour, not a count of the tree; the count (2 of 85 module
    files accepted no candidate path before, 0 after) was measured by asking the gate itself over
    every module file, and it is a measurement of the change, not something this test re-derives.
    """
    graph = reachability.import_graph(ROOT)
    with_files = {
        reachability.module_name(ROOT, path)
        for path in (ROOT / reachability.PACKAGE).rglob("*.py")
    }
    from_an_entry = reachability.reachable(graph)
    orphans_by_name = sorted(m for m in with_files if m not in from_an_entry)
    assert orphans_by_name == [], (
        "these module files are reached by no process entry point, so no path can be named for them "
        "unless they are entry points themselves: " + ", ".join(orphans_by_name)
    )
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


def test_the_walk_agrees_with_importlib(tmp_path: Path) -> None:
    """Every relative form is checked against the interpreter's own resolver, not against prose.

    ``importlib.util.resolve_name`` is the function the import system uses, so agreeing with it is
    agreement with the runtime rather than with a second copy of my reading. The no-package form is
    the one that had it backwards: the walk resolved ``import_module(".sibling")`` against the
    caller's package while the interpreter raises ``TypeError``. A test asserting the invented edge
    passed for as long as nobody asked the interpreter.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "import importlib\nmod = importlib.import_module('.feature', __package__)\n",
        "daedalus/feature.py": "VALUE = 7\n",
        "daedalus/sub/__init__.py": "",
        "daedalus/sub/loader.py": "import importlib\nmod = importlib.import_module('.sibling', package=__package__)\n",
        "daedalus/sub/pos.py": "import importlib\nmod = importlib.import_module('.sibling', __package__)\n",
        "daedalus/sub/strlit.py": "import importlib\nmod = importlib.import_module('.sibling', 'daedalus.sub')\n",
        "daedalus/sub/deep.py": "import importlib\nmod = importlib.import_module('..feature', package=__package__)\n",
        "daedalus/sub/loose.py": "import importlib\nmod = importlib.import_module('.sibling')\n",
        "daedalus/sub/sibling.py": "OK = True\n",
    })
    graph = reachability.import_graph(root)

    # (module, source form, the package the interpreter resolves it against)
    resolved_by_runtime = [
        ("daedalus.__main__", ".feature", "daedalus"),
        ("daedalus.sub.loader", ".sibling", "daedalus.sub"),
        ("daedalus.sub.pos", ".sibling", "daedalus.sub"),
        ("daedalus.sub.strlit", ".sibling", "daedalus.sub"),
        ("daedalus.sub.deep", "..feature", "daedalus.sub"),
    ]
    for module, dotted, package in resolved_by_runtime:
        want = importlib.util.resolve_name(dotted, package)
        assert want in graph[module], f"{module}: walk missed {want} (runtime resolves {dotted!r})"

    # The form that names no package: the interpreter refuses it, and so does the walk.
    with pytest.raises(TypeError):
        importlib.import_module(".sibling")
    assert ".sibling" in graph["daedalus.sub.loose"]
    assert "daedalus.sub.sibling" not in graph["daedalus.sub.loose"], (
        "the walk drew an edge for a call the interpreter refuses with TypeError"
    )
    # And the consequence, stated as behaviour: a module reachable only that way reads as unreached.
    loose_only = _tree(tmp_path / "looseonly", {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus import live\n",
        "daedalus/live.py": "import importlib\nmod = importlib.import_module('.dead')\n",
        "daedalus/dead.py": "",
    })
    assert "daedalus.dead" in reachability.unreachable_modules(loose_only), (
        "a module named only by a no-package relative call must not read as reached"
    )


def test_a_local_variable_named_extensions_is_not_the_registry(tmp_path: Path) -> None:
    """The registry is the tuple the extensions package iterates, not a name in a function body.

    The walk used to read any assignment to ``EXTENSIONS`` anywhere in the tree, and ``ast.walk``
    descends into function bodies. So one line inside an already-reached module —

        EXTENSIONS = ('daedalus.parasite',)

    as a local — created the edges, and a single proposal could name a module nothing imports as its
    own execution path: no merged step needed, unlike the entry-point rule. Both halves matter, so
    this test asserts both: the local is ignored, and the real registry is still read.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.app import serve\n",
        "daedalus/app.py": (
            "from daedalus.host import runner\n"
            "def serve():\n"
            "    EXTENSIONS = ('daedalus.parasite',)\n"
            "    return runner.run()\n"
        ),
        "daedalus/host/__init__.py": "",
        "daedalus/host/runner.py": "",
        "daedalus/parasite.py": "VALUE = 1\n",
    })
    graph = reachability.import_graph(root)
    assert "daedalus.parasite" not in graph["daedalus.app"], (
        "a local variable named EXTENSIONS created a reachability edge"
    )
    assert "daedalus.parasite" in reachability.unreachable_modules(root)

    # The other half: the module that declares and iterates the tuple is still read as a list of
    # literal imports. Without this the fix would pass by ignoring the registry altogether.
    listed = _tree(tmp_path / "listed", {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.extensions import install_all\n",
        "daedalus/extensions/__init__.py": (
            "EXTENSIONS = ('daedalus.ext.alpha',)\n"
            "def install_all():\n"
            "    for name in EXTENSIONS:\n"
            "        importlib.import_module(name)\n"
        ),
        "daedalus/ext/__init__.py": "",
        "daedalus/ext/alpha.py": "VALUE = 1\n",
    })
    listed_graph = reachability.import_graph(listed)
    assert "daedalus.ext.alpha" in listed_graph[reachability.EXTENSIONS_MODULE], (
        "the walk no longer reads the real extension registry"
    )
    assert reachability.unreachable_modules(listed) == []


def test_a_call_the_interpreter_cannot_make_draws_no_edge(tmp_path: Path) -> None:
    """Arity is checkable too: ``import_module`` takes one or two positional arguments, never three.

    ``import_module('.x', 'pkg', True)`` raises ``TypeError`` at run time, so no module is loadable
    from it; the walk drew the edge anyway. Same family as the missing-package form, and the same
    repair: keep the literal, draw nothing.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.loader import mod\n",
        "daedalus/loader.py": (
            "from importlib import import_module\n"
            "mod = import_module('.parasite', 'daedalus', True)\n"
        ),
        "daedalus/parasite.py": "VALUE = 1\n",
    })
    with pytest.raises(TypeError):
        importlib.import_module(".parasite", "daedalus", True)
    assert "daedalus.parasite" not in reachability.import_graph(root)["daedalus.loader"]
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_a_method_named_import_module_is_not_the_import_system(tmp_path: Path) -> None:
    """The receiver decides. Another object's ``import_module`` is not importlib's.

    ``registry.import_module('.x', 'pkg')`` resolved to a module the walk had no warrant for: the
    method may load nothing at all. That is an invented edge — it hides a module nothing imports
    instead of refusing a change that runs — and it is the same family as the missing-package form.
    ``importlib`` and a name aliased to it must still count, or the fix would throw away real edges.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.loader import registry\n",
        "daedalus/loader.py": (
            "import importlib as il\n"
            "import importlib\n"
            "class Registry:\n"
            "    def import_module(self, name, package=None):\n"
            "        return None\n"
            "registry = Registry()\n"
            "a = registry.import_module('.parasite', 'daedalus')\n"
            "b = importlib.import_module('.real', 'daedalus')\n"
            "c = il.import_module('.real', 'daedalus')\n"
        ),
        "daedalus/parasite.py": "VALUE = 1\n",
        "daedalus/real.py": "VALUE = 2\n",
    })
    edges = reachability.import_graph(root)["daedalus.loader"]
    assert "daedalus.parasite" not in edges, "a method named import_module was read as the import system"
    assert "daedalus.real" in edges, "importlib itself is no longer read"
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_a_call_the_interpreter_refuses_by_keyword_shape_draws_no_edge(tmp_path: Path) -> None:
    """Arity was not the only shape CPython refuses; keyword binding is the rest of it.

    ``import_module`` takes ``name`` (positionally or by keyword), at most one ``package``, and no
    other keyword. Each call below is a ``TypeError`` at run time, and each used to be read as a real
    import — the same invented edge as the three-positional form, one argument shape further along.
    The legal keyword-only form must still count, or the fix would throw away real edges.
    """
    # The interpreter's own verdict on the three shapes, so the test states why they draw nothing.
    for refused in (
        lambda: importlib.import_module(".parasite", "daedalus", package="daedalus"),
        lambda: importlib.import_module("daedalus.parasite", level=1),
        lambda: importlib.import_module("daedalus.parasite", name="q"),
    ):
        with pytest.raises(TypeError):
            refused()

    shapes = {
        "dup_package": "import_module('.parasite', 'daedalus', package='daedalus')",
        "unexpected_kw": "import_module('daedalus.parasite', level=1)",
        "dup_name": "import_module('daedalus.parasite', name='q')",
    }
    files = {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus import loader\n",
        "daedalus/parasite.py": "VALUE = 1\n",
        "daedalus/legal.py": "VALUE = 2\n",
        "daedalus/loader.py": "from importlib import import_module\n" + "".join(
            f"{label} = {call}\n" for label, call in shapes.items()
        ) + "kwonly = import_module(name='daedalus.legal')\n",
    }
    root = _tree(tmp_path, files)
    edges = reachability.import_graph(root)["daedalus.loader"]
    assert "daedalus.parasite" not in edges, "a call the interpreter refuses by keyword shape drew an edge"
    assert "daedalus.legal" in edges, "the legal keyword-only name= form is no longer read"
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_a_registry_declared_inside_a_function_is_not_the_registry(tmp_path: Path) -> None:
    """Module scope is part of the rule, not the filename alone.

    Restricting the registry read to ``daedalus.extensions`` closed the case of a local named
    ``EXTENSIONS`` in *another* module. The same escape remained inside that module: an unused
    function assigning a local of the same name. A top-level assignment in the registry module is
    still read — the fix must not be "ignore the registry".
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.extensions import install_all\n",
        "daedalus/extensions/__init__.py": (
            "import importlib\n"
            "EXTENSIONS = ('daedalus.ext.alpha',)\n"
            "def install_all():\n"
            "    for name in EXTENSIONS:\n"
            "        importlib.import_module(name)\n"
            "def _unused_helper():\n"
            "    EXTENSIONS = ('daedalus.parasite',)\n"
            "    return EXTENSIONS\n"
        ),
        "daedalus/ext/__init__.py": "",
        "daedalus/ext/alpha.py": "VALUE = 1\n",
        "daedalus/parasite.py": "VALUE = 2\n",
    })
    edges = reachability.import_graph(root)[reachability.EXTENSIONS_MODULE]
    assert "daedalus.ext.alpha" in edges, "the top-level registry is no longer read"
    assert "daedalus.parasite" not in edges, "a registry local inside a function created an edge"
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_branches_the_interpreter_never_enters_are_not_walked(tmp_path: Path) -> None:
    """``if TYPE_CHECKING:`` and ``if False:`` hold imports that never run.

    ``ast.walk`` visited them, so one line in any already-reached module —
    ``if TYPE_CHECKING: from daedalus.parasite import X`` — gave a module nothing imports an incoming
    edge and the gate accepted a single proposal. Pruning only ever removes edges, so the repair
    cannot accept something that does not run; what it must not do is remove real ones, which is why
    the ordinary conditional and the ``__main__`` guard are asserted here too.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.loader import live\n",
        "daedalus/loader.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from daedalus.parasite import Shape\n"
            "if False:\n"
            "    from daedalus.parasite import Other\n"
            "if True:\n"
            "    from daedalus.legal import Thing\n"
            "if __name__ == '__main__':\n"
            "    from daedalus.mainonly import Run\n"
        ),
        "daedalus/parasite.py": "VALUE = 1\n",
        "daedalus/legal.py": "Thing = 1\n",
        "daedalus/mainonly.py": "Run = 1\n",
    })
    edges = reachability.import_graph(root)["daedalus.loader"]
    assert "daedalus.parasite" not in edges, "an import in a branch the interpreter never enters drew an edge"
    assert "daedalus.legal" in edges, "an ordinary conditional import is no longer read"
    assert "daedalus.mainonly" in edges, "the __main__ guard is no longer read"
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_a_package_guessed_from_an_attribute_expression_is_not_drawn(tmp_path: Path) -> None:
    """The removed attribute branch could be restored without any test noticing; not any more.

    ``package=__package__.__name__`` reads an attribute of ``__package__``, which is a ``str`` in any
    ordinary module and raises ``AttributeError`` — and if a file rebinds ``__name__`` the answer
    exists only at run time. Either way the walk cannot name the package, so it keeps the literal and
    draws nothing. The branch that guessed one produced a package the source never names, which is the
    one thing this walk must not do.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.loader import x\n",
        "daedalus/loader.py": (
            "from importlib import import_module\n"
            "x = import_module('.parasite', package=__package__.__name__)\n"
        ),
        "daedalus/parasite.py": "VALUE = 1\n",
    })
    edges = reachability.import_graph(root)["daedalus.loader"]
    # The removed branch resolved against the caller's own module name, inventing this name.
    assert "daedalus.loader.parasite" not in edges
    assert "daedalus.parasite" not in edges
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_a_bare_call_counts_only_when_the_file_binds_the_name_from_importlib(tmp_path: Path) -> None:
    """The last way to name a module nothing imports: shadow the name.

    ``from importlib import import_module`` is a real form and must keep working. But a file may
    define ``def import_module(name)`` — a local helper that loads nothing — and the walk counted any
    bare call of that name as the import system's. A reviewer's counterexample did exactly that and
    the gate accepted a module nothing imports. The binding decides: the file's own ``from importlib
    import ...`` names count, anything else the file binds to that name does not.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus import honest, shadower, unbound\n",
        "daedalus/honest.py": (
            "from importlib import import_module\n"
            "x = import_module('.real', 'daedalus')\n"
        ),
        "daedalus/shadower.py": (
            "def import_module(name):\n"
            "    return None\n"
            "x = import_module('daedalus.parasite')\n"
        ),
        "daedalus/unbound.py": (
            "def load(import_module):\n"
            "    return import_module('daedalus.parasite')\n"
        ),
        "daedalus/real.py": "VALUE = 1\n",
        "daedalus/parasite.py": "VALUE = 2\n",
    })
    graph = reachability.import_graph(root)
    assert "daedalus.real" in graph["daedalus.honest"], "the real from-import form no longer counts"
    assert "daedalus.parasite" not in graph["daedalus.shadower"], "a local function named import_module drew an edge"
    assert "daedalus.parasite" not in graph["daedalus.unbound"], "a parameter named import_module drew an edge"
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_a_rebound_receiver_or_a_second_binding_of_the_name_draws_nothing(tmp_path: Path) -> None:
    """Every way a file can rebind the name, not only ``Assign``.

    The binding rule is only as good as its list of binding forms. A reviewer listed the ones a
    first version missed — a ``for`` target, a ``with ... as``, an ``except ... as``, a walrus, an
    ``AnnAssign``, a ``del``, and rebinding ``importlib`` itself. Each of them makes the name something
    other than the import system's function, and each one is a line of a single proposal.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": (
            "from daedalus import fortarget, withtarget, excepttarget, walrus, annotated, deleted, rebound\n"
        ),
        "daedalus/fortarget.py": (
            "from importlib import import_module\n"
            "for import_module in [None]:\n"
            "    pass\n"
            "x = import_module('daedalus.parasite')\n"
        ),
        "daedalus/withtarget.py": (
            "from importlib import import_module\n"
            "with open('/dev/null') as import_module:\n"
            "    pass\n"
            "x = import_module('daedalus.parasite')\n"
        ),
        "daedalus/excepttarget.py": (
            "from importlib import import_module\n"
            "try:\n"
            "    pass\n"
            "except Exception as import_module:\n"
            "    pass\n"
            "x = import_module('daedalus.parasite')\n"
        ),
        "daedalus/walrus.py": (
            "from importlib import import_module\n"
            "if (import_module := None) is None:\n"
            "    pass\n"
            "x = import_module('daedalus.parasite')\n"
        ),
        "daedalus/annotated.py": (
            "from importlib import import_module\n"
            "import_module: object = None\n"
            "x = import_module('daedalus.parasite')\n"
        ),
        "daedalus/deleted.py": (
            "from importlib import import_module\n"
            "del import_module\n"
            "x = import_module('daedalus.parasite')\n"
        ),
        "daedalus/rebound.py": (
            "import importlib\n"
            "importlib = None\n"
            "x = importlib.import_module('daedalus.parasite')\n"
        ),
        "daedalus/parasite.py": "VALUE = 1\n",
    })
    graph = reachability.import_graph(root)
    # Membership of the whole dotted name: ``in`` on a set of names is exact, and a substring test
    # would pass while the edge sits right there (the first version of this assertion did exactly
    # that, and the mutation table caught it).
    wrong = sorted(m for m in graph if "daedalus.parasite" in graph[m])
    assert wrong == [], f"a rebound name still drew an edge in: {', '.join(wrong)}"


def test_a_module_loaded_only_by_discovery_is_reached_but_an_underscore_one_is_not(tmp_path: Path) -> None:
    """Discovery counts what the loader loads, not what happens to sit in the directory.

    ``discover_tools`` imports every direct submodule of ``daedalus.tools`` except the
    underscore-prefixed ones (``_common`` is imported by name where it is needed). Counting them as
    discovered made ``daedalus/tools/_whatever.py`` read as reached although nothing imports it: a
    one-line, one-proposal way through the gate, and pre-existing.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.tools import discover_tools\n",
        "daedalus/tools/__init__.py": (
            "import importlib, pkgutil\n"
            "def discover_tools():\n"
            "    for info in pkgutil.iter_modules(__path__):\n"
            "        if not info.name.startswith('_'):\n"
            "            importlib.import_module(f'{__name__}.{info.name}')\n"
        ),
        "daedalus/tools/discovered.py": "VALUE = 1\n",
        "daedalus/tools/_private.py": "VALUE = 2\n",
        "daedalus/tools/_parasite.py": "VALUE = 3\n",
    })
    unreachable = reachability.unreachable_modules(root)
    assert "daedalus.tools.discovered" not in unreachable, "a discovered module stopped counting"
    assert "daedalus.tools._parasite" in unreachable, (
        "an underscore-prefixed module counts as discovered although the loader never imports it"
    )


def test_a_comparison_of_literals_is_a_dead_branch_too(tmp_path: Path) -> None:
    """``if 1 == 2:`` and ``while False:`` never run; walking them is the same one-line escape.

    Pruning only ``False`` and ``TYPE_CHECKING`` left the hole open through an obviously-false
    comparison. A comparison of two literal constants is decidable from the source, so it is pruned
    like the others; a comparison involving a name is not, and is still walked.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus import loader\n",
        "daedalus/loader.py": (
            "if 1 == 2:\n"
            "    from daedalus import parasite\n"
            "while False:\n"
            "    from daedalus import parasite2\n"
            "if 1 == 1:\n"
            "    from daedalus import legal\n"
            "flag = True\n"
            "if flag:\n"
            "    from daedalus import legal2\n"
        ),
        "daedalus/parasite.py": "VALUE = 1\n",
        "daedalus/parasite2.py": "VALUE = 2\n",
        "daedalus/legal.py": "VALUE = 3\n",
        "daedalus/legal2.py": "VALUE = 4\n",
    })
    edges = reachability.import_graph(root)["daedalus.loader"]
    unreachable = reachability.unreachable_modules(root)
    assert "daedalus.parasite" in unreachable and "daedalus.parasite2" in unreachable
    assert "daedalus.legal" not in unreachable and "daedalus.legal2" not in unreachable
    assert "daedalus.parasite" not in edges


def test_a_registry_in_a_module_that_does_not_iterate_it_is_not_the_registry(tmp_path: Path) -> None:
    """The module restriction has to hold on its own, not only together with the scope check.

    A top-level ``EXTENSIONS`` in some other module is a list, not the starter's registry: nothing
    iterates it and loads the names. Without this test either guard could be removed without a failure,
    which means neither was load-bearing by itself.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus import elsewhere\n",
        "daedalus/elsewhere.py": "EXTENSIONS = ('daedalus.parasite',)\n",
        "daedalus/parasite.py": "VALUE = 1\n",
    })
    assert "daedalus.parasite" not in reachability.import_graph(root)["daedalus.elsewhere"]
    assert "daedalus.parasite" in reachability.unreachable_modules(root)


def test_the_gate_carries_its_own_boot_set() -> None:
    """The self-naming exception must not be a rule the audited tree can extend.

    The gate refuses "the changed module itself" except for a process entry point. That set lived in
    ``reachability.ENTRY_POINTS`` — a host module a proposal may change — so a change that added a
    module and added its name to that tuple in the same tree made a module nothing imports proposable
    (verified against a tree copy). The set now lives here, and this test holds the two lists
    together: if someone adds a real entry point they must say so in both files, and if the two drift
    the suite fails instead of the gate quietly widening.
    """
    assert set(SELF_NAMABLE) == set(reachability.ENTRY_POINTS), (
        "the gate's boot set and the reachability entry points have drifted: reconcile them "
        "deliberately, because this set decides what may name itself"
    )


def test_a_widened_entry_point_tuple_cannot_widen_the_gate(tmp_path: Path, monkeypatch) -> None:
    """The hole this closes, with the state it needs.

    It is a two-step hole, not a one-change one, and saying so is part of the test: the gate imports
    ``reachability`` from the running package, so a proposed tree cannot widen the rule it is judged
    by. But once a change adding a name to ``ENTRY_POINTS`` is merged, a later proposal can add a
    module nothing imports under that name and be accepted.

    Step one has to be reproduced faithfully, because two weaker versions of this test prove nothing.
    Patching ``reachability.ENTRY_POINTS`` alone is not enough: ``reachable()`` captured that tuple as
    its default when it was defined, so the walk would still start from the old entry points, read the
    parasite as unreachable, and refuse on the reachability check rather than on the rule. Reloading
    the module does not help either — it re-executes the source and resets the constant. What a merged
    change actually leaves behind is the widened tuple *as the function's default*, so that is what
    this test installs, and it asserts that state before it asserts the refusal: the walk must report
    the parasite as reached, or the refusal below would prove nothing about the gate's own rule.
    """
    root = _tree(tmp_path, {
        "daedalus/__init__.py": "",
        "daedalus/__main__.py": "from daedalus.app import serve\n",
        "daedalus/app.py": "from daedalus.host import runner\n",
        "daedalus/host/__init__.py": "",
        "daedalus/host/runner.py": "",
        "daedalus/parasite.py": "VALUE = 1\n",
    })
    widened = (*reachability.ENTRY_POINTS, "daedalus.parasite")
    # The coupling below is to an implementation detail (the boot set is a default argument of
    # ``reachable``). Guard it out loud: if that changes, this test must fail here, naming what to fix,
    # rather than quietly stop reproducing step one and prove nothing.
    defaults = reachability.reachable.__defaults__
    assert defaults and defaults[0] is reachability.ENTRY_POINTS, (
        "``reachable()`` no longer captures ``ENTRY_POINTS`` as its default argument, so this test no "
        "longer reproduces the merged state it is about. Update the way step one is installed (the "
        "point is: the widened tuple must be what the running walk actually starts from), then this "
        "assertion."
    )
    monkeypatch.setattr(reachability, "ENTRY_POINTS", widened)
    monkeypatch.setattr(reachability.reachable, "__defaults__", (widened,))
    assert "daedalus.parasite" in reachability.reachable(reachability.import_graph(root)), (
        "the step-one state was not reproduced: the walk does not start from the widened tuple, "
        "so the refusal below would prove nothing about the gate's own rule"
    )
    with pytest.raises(ProposalRefused, match="is the changed module itself"):
        relevance_gate(root, ["daedalus/parasite.py"], "daedalus.parasite")
    # Control: the module that really is a boot entry point may still name itself.
    relevance_gate(root, ["daedalus/__main__.py"], "daedalus.__main__")
