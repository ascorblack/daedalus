"""Skill evaluation harness — the four-check minimal validation loop.

A skill earns registration only if it passes all four checks in a single run:

1. **Necessity** — the base agent, WITHOUT the skill, does not solve the
   decisive task ``P``. "Solves ``P``" is the benefit predicate (exit 0 with a
   valid artifact), so necessity is its negation: if the base model already
   produces a valid artifact, the skill is redundant — a placebo that only
   parasitises the base model's own ability.
2. **Benefit** — WITH the skill, ``P`` yields a valid artifact (exit 0 and a
   non-None artifact, not merely a "loaded" status).
3. **Invariance** — after running ``P`` the workspace diff, minus the skill's
   *declared* artifacts, is empty (no pollution), and a neutral canary task
   ``C`` is unchanged before vs after ``P`` (exit code, artifact and workspace
   diff all match). The canary is run immediately after ``P``-with-skill, so it
   reflects ``P``'s context poisoning specifically — a later near-miss cannot
   mask or mix into it.
4. **Selectivity** — a lexically similar near-miss ``N1`` does NOT trigger the
   skill and costs no more than the no-skill baseline plus a small allowance.
   When the no-skill baseline cost is zero (not metered) the cost clause is
   skipped and only the trigger clause is checked.

The invariance check compares against the declared artifacts, not against an
empty tree: a skill whose job is to create files (a design skill writing
``index.html``) is not pollution for doing so. Declared artifacts and the
runner's diff entries are normalised with ``posixpath.normpath`` before
comparison, so ``./out/index.html`` and ``out/index.html`` match.

The harness takes an injected ``run_agent`` callable so it can be exercised in
CI with a deterministic mock; the host supplies the real runner. The canary's
``artifact`` is compared with ``==`` across runs, so the runner must return a
comparable value for the canary task.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from posixpath import normpath
from typing import Protocol


@dataclass(frozen=True)
class RunResult:
    """The outcome of one agent run of a task, with or without the skill.

    ``artifact`` is compared with ``==`` across runs in the canary check, so the
    runner should return a value with a meaningful ``__eq__`` for that task.
    """

    exit_code: int
    artifact: object | None = None
    skill_invoked: bool = False
    cost: float = 0.0
    workspace_diff: tuple[str, ...] = ()


class AgentRunner(Protocol):
    """Maps ``(task, *, with_skill=...)`` to a :class:`RunResult`."""

    def __call__(self, task: str, *, with_skill: bool) -> RunResult: ...


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    passed: bool
    detail: str


@dataclass
class EvalReport:
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    def failed(self) -> list[str]:
        return [o.name for o in self.outcomes if not o.passed]

    def summary(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        parts = "; ".join(f"{o.name}={'ok' if o.passed else 'FAIL'}" for o in self.outcomes)
        return f"{mark}: {parts}"


class SkillEvalHarness:
    """Runs the four checks once and returns an :class:`EvalReport`.

    ``cost_allowance`` is the relative headroom over the no-skill baseline the
    near-miss may spend; ``cost_floor`` is an absolute token floor so a 15%
    allowance on a tiny baseline does not become pure noise.
    """

    def __init__(self, runner: AgentRunner, *, cost_allowance: float = 0.15, cost_floor: float = 0.0) -> None:
        self._run = runner
        self._cost_allowance = cost_allowance
        self._cost_floor = cost_floor

    def evaluate(
        self,
        decisive_p: str,
        near_miss_n1: str,
        canary_c: str,
        declared_artifacts: Sequence[str] = (),
    ) -> EvalReport:
        declared = frozenset(normpath(d) for d in declared_artifacts)

        # Canary baseline: capture C's behaviour before the skill has run.
        c_pre = self._run(canary_c, with_skill=False)

        # 1. Necessity: the base agent must not solve P without the skill.
        p_no = self._run(decisive_p, with_skill=False)
        solves_p = p_no.exit_code == 0 and p_no.artifact is not None
        necessity = not solves_p

        # 2. Benefit: with the skill, P must produce a valid artifact.
        p_yes = self._run(decisive_p, with_skill=True)
        benefit = p_yes.exit_code == 0 and p_yes.artifact is not None

        # 3. Invariance: no undeclared pollution, and C is unchanged after P.
        #    c_post runs immediately after p_yes so it reflects P's context
        #    poisoning specifically (a later near-miss cannot mask or mix in).
        unexpected = tuple(d for d in p_yes.workspace_diff if normpath(d) not in declared)
        c_post = self._run(canary_c, with_skill=False)
        canary_stable = (
            c_pre.exit_code == c_post.exit_code
            and c_pre.artifact == c_post.artifact
            and c_pre.workspace_diff == c_post.workspace_diff
        )
        invariance = (not unexpected) and canary_stable

        # 4. Selectivity: N1 must not trigger the skill and must stay cheap.
        base_n1 = self._run(near_miss_n1, with_skill=False)
        n1_yes = self._run(near_miss_n1, with_skill=True)
        triggered = n1_yes.skill_invoked
        if base_n1.cost <= 0:
            # Baseline not metered: only the trigger clause is meaningful.
            cost_ok = True
            cost_desc = "baseline unmetered"
        else:
            budget = max(base_n1.cost * (1.0 + self._cost_allowance), self._cost_floor)
            cost_ok = n1_yes.cost <= budget
            cost_desc = f"cost {n1_yes.cost:.3f} <= budget {budget:.3f}"
        selectivity = (not triggered) and cost_ok

        return EvalReport(
            outcomes=[
                CheckOutcome(
                    "necessity",
                    necessity,
                    "base agent does not solve P without the skill"
                    if necessity
                    else "base agent already solves P — skill is redundant",
                ),
                CheckOutcome(
                    "benefit",
                    benefit,
                    "P with the skill yields a valid artifact"
                    if benefit
                    else "P with the skill failed or produced no artifact",
                ),
                CheckOutcome(
                    "invariance",
                    invariance,
                    "workspace clean modulo declared artifacts; canary unchanged"
                    if invariance
                    else (
                        f"undeclared diff={list(unexpected)}; "
                        f"canary pre={c_pre.exit_code}/{c_pre.artifact!r}/{c_pre.workspace_diff} "
                        f"post={c_post.exit_code}/{c_post.artifact!r}/{c_post.workspace_diff}"
                    ),
                ),
                CheckOutcome(
                    "selectivity",
                    selectivity,
                    f"N1 quiet and within budget ({cost_desc})"
                    if selectivity
                    else (
                        "N1 triggered the skill on a near-miss"
                        if triggered
                        else f"N1 over budget ({cost_desc})"
                    ),
                ),
            ]
        )


__all__ = ["AgentRunner", "CheckOutcome", "EvalReport", "RunResult", "SkillEvalHarness"]
