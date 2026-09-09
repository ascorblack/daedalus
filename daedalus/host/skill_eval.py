"""Skill evaluation harness — the four-check minimal validation loop.

A skill earns registration only if it passes all four checks in a single run:

1. **Necessity** — the base agent, WITHOUT the skill, fails the decisive task
   ``P``. If the base model already solves ``P``, the skill is redundant: a
   placebo that only parasitises the base model's own ability.
2. **Benefit** — WITH the skill, ``P`` yields a valid artifact (a real object,
   not merely a "loaded" status).
3. **Selectivity** — a lexically similar near-miss ``N1`` does NOT trigger the
   skill and costs no more than the no-skill baseline plus a small allowance.
4. **Invariance** — after running ``P`` the workspace diff, minus the skill's
   *declared* artifacts, is empty (no pollution), and a neutral canary task
   ``C`` behaves the same as before the skill ran (no context poisoning /
   sticky steering).

The harness takes an injected ``run_agent`` callable so it can be exercised in
CI with a deterministic mock; the host supplies the real runner. The canary is
run before and after the decisive ``P``-with-skill run so a skill that leaves a
directive in the context window is caught even when its artifact is correct.

The invariance check compares against the declared artifacts, not against an
empty tree: a skill whose job is to create files (a design skill writing
``index.html``) is not pollution for doing so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class RunResult:
    """The outcome of one agent run of a task, with or without the skill."""

    exit_code: int
    artifact: object | None = None
    skill_invoked: bool = False
    cost: float = 0.0
    workspace_diff: tuple[str, ...] = ()


# A runner maps ``(task, *, with_skill=...)`` to a :class:`RunResult`.
AgentRunner = Callable[..., RunResult]


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
        declared = frozenset(declared_artifacts)

        # Canary baseline: capture C's behaviour before the skill has run.
        c_pre = self._run(canary_c, with_skill=False)

        # 1. Necessity: the base agent must fail P without the skill.
        p_no = self._run(decisive_p, with_skill=False)
        necessity = p_no.exit_code != 0

        # 2. Benefit: with the skill, P must produce a valid artifact.
        p_yes = self._run(decisive_p, with_skill=True)
        benefit = p_yes.exit_code == 0 and p_yes.artifact is not None

        # 3. Selectivity: N1 must not trigger the skill and must stay cheap.
        base_n1 = self._run(near_miss_n1, with_skill=False)
        n1_yes = self._run(near_miss_n1, with_skill=True)
        budget = max(base_n1.cost * (1.0 + self._cost_allowance), self._cost_floor)
        selectivity = (not n1_yes.skill_invoked) and n1_yes.cost <= budget

        # 4. Invariance: no undeclared pollution, and C is unchanged after P.
        unexpected = tuple(d for d in p_yes.workspace_diff if d not in declared)
        c_post = self._run(canary_c, with_skill=False)
        canary_stable = c_pre.exit_code == c_post.exit_code and c_pre.artifact == c_post.artifact
        invariance = (not unexpected) and canary_stable

        return EvalReport(
            outcomes=[
                CheckOutcome(
                    "necessity",
                    necessity,
                    "base agent fails P without the skill"
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
                    "selectivity",
                    selectivity,
                    f"N1 quiet and within budget (cost {n1_yes.cost:.3f} <= {budget:.3f})"
                    if selectivity
                    else f"N1 triggered={n1_yes.skill_invoked}, cost {n1_yes.cost:.3f} > budget {budget:.3f}",
                ),
                CheckOutcome(
                    "invariance",
                    invariance,
                    "workspace clean modulo declared artifacts; canary unchanged"
                    if invariance
                    else (
                        f"undeclared diff={list(unexpected)}; "
                        f"canary pre={c_pre.exit_code}/{c_pre.artifact!r} "
                        f"post={c_post.exit_code}/{c_post.artifact!r}"
                    ),
                ),
            ]
        )


__all__ = ["AgentRunner", "CheckOutcome", "EvalReport", "RunResult", "SkillEvalHarness"]
