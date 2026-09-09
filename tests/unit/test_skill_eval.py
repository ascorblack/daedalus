"""Skill evaluation harness: the four-check minimal validation loop."""

from __future__ import annotations

from daedalus.host.skill_eval import RunResult, SkillEvalHarness


def ok(exit_code: int = 0, artifact=None, invoked: bool = False, cost: float = 0.0, diff=()) -> RunResult:
    return RunResult(
        exit_code=exit_code,
        artifact=artifact,
        skill_invoked=invoked,
        cost=cost,
        workspace_diff=tuple(diff),
    )


class ScriptedRunner:
    """A deterministic mock runner: one scripted result per (task, with_skill)."""

    def __init__(self, table: dict[tuple[str, bool], RunResult]) -> None:
        self._table = table

    def __call__(self, task: str, *, with_skill: bool) -> RunResult:
        return self._table[(task, with_skill)]


def passing_table() -> dict[tuple[str, bool], RunResult]:
    return {
        ("canary", False): ok(0, artifact="C"),
        ("P", False): ok(1),  # base agent fails P -> necessity passes
        ("P", True): ok(0, artifact="A", diff=["out/index.html"]),
        ("N1", False): ok(0, cost=10.0),
        ("N1", True): ok(0, invoked=False, cost=11.0),  # 11.0 <= 10*1.15
    }


def test_all_four_pass() -> None:
    h = SkillEvalHarness(ScriptedRunner(passing_table()))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.passed
    assert rep.failed() == []
    assert rep.summary().startswith("PASS")


def test_necessity_fails_when_base_solves_p() -> None:
    table = passing_table()
    table[("P", False)] = ok(0, artifact="A")  # base agent already solves P
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "necessity" in rep.failed()
    assert not rep.passed


def test_benefit_fails_when_no_artifact() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact=None)  # no artifact
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "benefit" in rep.failed()


def test_benefit_fails_when_p_errors() -> None:
    table = passing_table()
    table[("P", True)] = ok(1, artifact="A")  # non-zero exit
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "benefit" in rep.failed()


def test_selectivity_fails_when_n1_triggered() -> None:
    table = passing_table()
    table[("N1", True)] = ok(0, invoked=True, cost=11.0)  # skill triggered on near-miss
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "selectivity" in rep.failed()


def test_selectivity_fails_when_n1_too_expensive() -> None:
    table = passing_table()
    table[("N1", True)] = ok(0, invoked=False, cost=20.0)  # way over budget
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "selectivity" in rep.failed()


def test_cost_floor_guards_tiny_baseline() -> None:
    # Baseline cost is 0; a 15% allowance would be 0, but the floor (1.0) sets the budget.
    table = passing_table()
    table[("N1", False)] = ok(0, cost=0.0)
    table[("N1", True)] = ok(0, invoked=False, cost=0.5)  # 0.5 <= floor 1.0 -> ok
    h = SkillEvalHarness(ScriptedRunner(table), cost_floor=1.0)
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "selectivity" not in rep.failed()


def test_invariance_fails_on_undeclared_diff() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact="A", diff=["out/index.html", "rogue.tmp"])
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "invariance" in rep.failed()


def test_invariance_passes_when_diff_is_declared() -> None:
    # The skill's declared artifact is out/index.html; the diff matches it exactly.
    h = SkillEvalHarness(ScriptedRunner(passing_table()))
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "invariance" not in rep.failed()


class DriftRunner:
    """Simulates context poisoning: the canary behaves differently after P ran."""

    def __init__(self) -> None:
        self._canary_calls = 0

    def __call__(self, task: str, *, with_skill: bool) -> RunResult:
        if task == "canary":
            self._canary_calls += 1
            return ok(0, artifact="C" if self._canary_calls == 1 else "C-drifted")
        return passing_table()[(task, with_skill)]


def test_invariance_fails_on_canary_drift() -> None:
    h = SkillEvalHarness(DriftRunner())
    rep = h.evaluate("P", "N1", "canary", declared_artifacts=["out/index.html"])
    assert "invariance" in rep.failed()
    assert "C-drifted" in rep.outcomes[3].detail


def test_summary_marks_fail() -> None:
    table = passing_table()
    table[("P", False)] = ok(0, artifact="A")
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate("P", "N1", "canary")
    assert rep.summary().startswith("FAIL")
