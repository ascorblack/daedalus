"""Skill evaluation harness: the four-check minimal validation loop."""

from __future__ import annotations

from daedalus.host.skill_eval import RunResult, SkillEvalHarness

# Outcomes are reported in execution order: necessity, benefit, invariance, selectivity.
NEC, BEN, INV, SEL = 0, 1, 2, 3


def ok(exit_code: int = 0, artifact: object = None, invoked: bool = False, cost: float = 0.0, diff=()) -> RunResult:
    return RunResult(exit_code, artifact, invoked, cost, tuple(diff))


class ScriptedRunner:
    """Deterministic runner: results keyed by (task, with_skill)."""

    def __init__(self, table: dict) -> None:
        self.table = table
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, task: str, *, with_skill: bool) -> RunResult:
        self.calls.append((task, with_skill))
        return self.table[(task, with_skill)]


def passing_table() -> dict:
    return {
        ("canary", False): ok(0, artifact="C"),
        ("canary", True): ok(0, artifact="C"),
        ("P", False): ok(1, artifact=None),
        ("P", True): ok(0, artifact="A", invoked=True, diff=("out/index.html",)),
        ("N1", False): ok(0, cost=10.0),
        ("N1", True): ok(0, invoked=False, cost=11.0),
    }


def test_all_four_pass() -> None:
    h = SkillEvalHarness(ScriptedRunner(passing_table()))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.passed
    assert rep.summary().startswith("PASS")
    assert rep.failed() == []


def test_necessity_fails_when_base_solves_p() -> None:
    table = passing_table()
    table[("P", False)] = ok(0, artifact="A")  # base model already solves P
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["necessity"]
    assert "redundant" in rep.outcomes[NEC].detail


def test_necessity_passes_when_base_exits_0_but_no_artifact() -> None:
    table = passing_table()
    table[("P", False)] = ok(0, artifact=None)  # exit 0 but no valid artifact: not a solution
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "necessity" not in rep.failed()


def test_benefit_fails_when_no_artifact() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact=None, invoked=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary")
    assert rep.failed() == ["benefit"]


def test_benefit_fails_when_p_errors() -> None:
    table = passing_table()
    table[("P", True)] = ok(1, artifact=None, invoked=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary")
    assert rep.failed() == ["benefit"]


def test_selectivity_fails_when_n1_triggered() -> None:
    table = passing_table()
    table[("N1", True)] = ok(0, invoked=True, cost=11.0)  # triggered, but cheap
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["selectivity"]
    assert "triggered" in rep.outcomes[SEL].detail  # message blames the trigger, not the cost


def test_selectivity_fails_when_n1_too_expensive() -> None:
    table = passing_table()
    table[("N1", True)] = ok(0, invoked=False, cost=20.0)  # quiet but 2x baseline
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["selectivity"]
    assert "over budget" in rep.outcomes[SEL].detail


def test_cost_clause_skipped_when_baseline_unmetered() -> None:
    table = passing_table()
    table[("N1", False)] = ok(0, cost=0.0)  # baseline not metered
    table[("N1", True)] = ok(0, invoked=False, cost=5.0)  # positive cost, clause skipped
    h = SkillEvalHarness(ScriptedRunner(table))  # default floor
    rep = h.evaluate("P", "N1", "canary")
    assert "selectivity" not in rep.failed()
    assert "unmetered" in rep.outcomes[SEL].detail


def test_cost_floor_guards_tiny_baseline() -> None:
    table = passing_table()
    table[("N1", False)] = ok(0, cost=0.1)  # tiny baseline: 15% headroom is noise
    table[("N1", True)] = ok(0, invoked=False, cost=0.2)  # over 0.115, within floor 0.5
    h = SkillEvalHarness(ScriptedRunner(table), cost_floor=0.5)
    rep = h.evaluate("P", "N1", "canary")
    assert "selectivity" not in rep.failed()


def test_invariance_fails_on_undeclared_diff() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact="A", invoked=True, diff=("out/index.html", "rogue.tmp"))
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "rogue.tmp" in rep.outcomes[INV].detail


def test_invariance_passes_with_path_normalized_declared_artifact() -> None:
    table = passing_table()
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["./out/index.html"])
    assert rep.passed  # ./out/index.html normalises to the diff entry out/index.html


def test_invariance_fails_on_canary_artifact_drift() -> None:
    class DriftRunner(ScriptedRunner):
        """Simulates context poisoning: the canary's output changes after P."""

        def __init__(self) -> None:
            super().__init__(passing_table())
            self.canary_calls = 0

        def __call__(self, task: str, *, with_skill: bool) -> RunResult:
            if task == "canary":
                self.canary_calls += 1
                return ok(0, artifact="C" if self.canary_calls == 1 else "C-drifted")
            return self.table[(task, with_skill)]

    h = SkillEvalHarness(DriftRunner())
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "C-drifted" in rep.outcomes[INV].detail


def test_invariance_fails_on_canary_workspace_drift() -> None:
    class WorkspaceDriftRunner(ScriptedRunner):
        """Same canary artifact, but the canary starts writing files after P."""

        def __init__(self) -> None:
            super().__init__(passing_table())
            self.canary_calls = 0

        def __call__(self, task: str, *, with_skill: bool) -> RunResult:
            if task == "canary":
                self.canary_calls += 1
                return ok(0, artifact="C", diff=() if self.canary_calls == 1 else ("canary.tmp",))
            return self.table[(task, with_skill)]

    h = SkillEvalHarness(WorkspaceDriftRunner())
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]


def test_summary_marks_fail() -> None:
    table = passing_table()
    table[("P", True)] = ok(1, artifact=None, invoked=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary")
    assert not rep.passed
    assert rep.summary().startswith("FAIL")
    assert "benefit=FAIL" in rep.summary()
