"""Skill evaluation harness — the four-check minimal validation loop.

A skill earns registration only if it passes all four checks in a single run:

1. **Necessity** — the base agent, WITHOUT the skill, does not solve the
   decisive task ``P``. "Solves ``P``" is the benefit predicate (exit 0 with a
   valid artifact), so necessity is its negation: if the base model already
   produces a valid artifact, the skill is redundant — a placebo that only
   parasitises the base model's own ability.
2. **Benefit** — WITH the skill, ``P`` yields a valid artifact (exit 0 and a
   non-None artifact, not merely a "loaded" status).
3. **Invariance** — three sub-clauses, all must hold:
   a. *No undeclared pollution*: after running ``P`` the workspace diff, minus
      the skill's *declared* artifacts, is empty.
   b. *Canary stability*: a neutral canary task ``C`` is unchanged before vs
      after ``P`` (exit code, artifact and workspace diff all match). The
      canary baseline is captured immediately before ``P``-with-skill (after
      the base run) and the canary is re-run immediately after, so the
      comparison isolates exactly what the skill run did to the context.
   c. *Append attribution*: the decisive artifact is attributable to THIS run.
      The runner reports the artifact target's ``pre_state`` and ``post_state``
      bytes; the harness checks ``post_state == pre_state + frame`` (exact
      append), or ``post_state == frame`` when the target did not pre-exist
      (``pre_state is None``). Keyed on ``H0 = SHA256(pre_state)``. This rules
      out replay, reordering and pre-existing duplicates: a skill that merely
      reads an artifact that was already there produces no append, so the
      check fails. A failed compare-and-swap on ``H0`` (a concurrent writer
      changed the pre-state between snapshot and write) fails the run — the
      race is part of the verification, not a side channel. An empty frame on a
      pre-existing target (a read-only replay) also fails. When the runner does
      not meter ``pre_state``/``post_state`` (``state_metered`` is ``False`` —
      the CI mock and legacy runners), this sub-clause is skipped, exactly as
      the cost clause is skipped when the baseline is unmetered.
4. **Selectivity** — a lexically similar near-miss ``N1`` does NOT trigger the
   skill and costs no more than the no-skill baseline plus a small allowance.
   When the no-skill baseline cost is zero (not metered) the cost clause is
   skipped and only the trigger clause is checked.

The invariance diff sub-clause compares against the declared artifacts, not
against an empty tree: a skill whose job is to create files (a design skill
writing ``index.html``) is not pollution for doing so. Declared artifacts and
the runner's diff entries are normalised with ``posixpath.normpath`` before
comparison, so ``./out/index.html`` and ``out/index.html`` match.

The harness takes an injected ``run_agent`` callable so it can be exercised in
CI with a deterministic mock; the host supplies the real runner. The canary's
``artifact`` is compared with ``==`` across runs, so the runner must return a
comparable value for the canary task. For the append-attribution sub-clause the
runner must, for the decisive ``P``-with-skill run, report ``pre_state``/
``post_state`` (bytes of the artifact target before/after the run, ``None`` if
the target did not exist), set ``state_metered=True`` to say it actually metered
that state, and set ``cas_failed`` if a compare-and-swap on the pre-state hash
lost to a concurrent writer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from posixpath import normpath
from typing import Protocol


@dataclass(frozen=True)
class RunResult:
    """The outcome of one agent run of a task, with or without the skill.

    ``artifact`` is compared with ``==`` across runs in the canary check, so the
    runner should return a value with a meaningful ``__eq__`` for that task.

    ``pre_state``/``post_state`` are the bytes of the decisive artifact target
    before/after this run (``None`` if the target did not exist). They feed the
    invariance append-attribution sub-clause. ``state_metered`` tells the
    harness that this run actually metered byte-level state: when it is
    ``False`` (the default, for legacy and CI mock runners) the sub-clause is
    skipped and reported as unmetered, even if ``pre_state``/``post_state`` are
    both ``None``. A runner that meters and reports the target absent both
    before and after a claimed artifact therefore sets ``state_metered=True``
    and is caught as a create mismatch, not silently skipped. ``cas_failed``
    is set by the runner when a compare-and-swap on the pre-state hash lost to
    a concurrent writer; it is honoured only when ``state_metered`` is set.
    """

    exit_code: int
    artifact: object | None = None
    skill_invoked: bool = False
    cost: float = 0.0
    workspace_diff: tuple[str, ...] = ()
    pre_state: bytes | None = None
    post_state: bytes | None = None
    cas_failed: bool = False
    state_metered: bool = False


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


def _frame_bytes(artifact: object) -> bytes:
    """The artifact's byte frame for the append-attribution sub-clause."""
    if isinstance(artifact, bytes):
        return artifact
    if isinstance(artifact, str):
        return artifact.encode("utf-8")
    raise TypeError(f"artifact must be bytes or str for append attribution, got {type(artifact).__name__}")


def _state_desc(state: bytes | None) -> str:
    """A short, non-leaking description of a byte state for detail strings.

    Reports length and a 12-hex SHA-256 prefix — enough to debug a mismatch
    without dumping the full artifact bytes (which may be large or sensitive)
    into the report.
    """
    if state is None:
        return "None"
    return f"len={len(state)} sha256={hashlib.sha256(state).hexdigest()[:12]}"


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

    def _check_attribution(self, p_yes: RunResult) -> tuple[bool, str]:
        """Invariance sub-clause c: the decisive artifact is attributable to this run.

        ``post_state`` must equal ``pre_state + frame`` (exact append), or just
        ``frame`` when the target did not pre-exist. Keyed on ``H0 =
        SHA256(pre_state)``; a failed CAS (concurrent writer) fails the run.
        Skipped (treated as passing) only when the runner does not meter state
        (``state_metered`` is ``False``); a metered run with the target absent
        both before and after a claimed artifact is a create mismatch. An empty
        frame on a pre-existing target is a replay and fails. Detail strings
        report lengths and hash prefixes, never full state bytes.
        """
        if not p_yes.state_metered:
            return True, "attribution unmetered"
        if p_yes.cas_failed:
            return False, "CAS failed: concurrent writer changed the pre-state"
        if p_yes.artifact is None:
            return True, "no artifact to attribute"
        try:
            frame = _frame_bytes(p_yes.artifact)
        except TypeError as e:
            return False, f"cannot form byte frame: {e}"
        if p_yes.pre_state is None:
            # Create: target did not pre-exist; post must be exactly the frame.
            expected = frame
            h0: str | None = None
            mode = "create"
        else:
            if not frame:
                return False, "empty frame: no bytes appended (replay of pre-existing target)"
            expected = p_yes.pre_state + frame
            h0 = hashlib.sha256(p_yes.pre_state).hexdigest()
            mode = "append"
        if p_yes.post_state == expected:
            return True, f"{mode} verified (H0={h0[:12] if h0 else 'n/a'})"
        return False, (
            f"{mode} mismatch: expected {_state_desc(expected)} got "
            f"{_state_desc(p_yes.post_state)} (H0={h0[:12] if h0 else 'n/a'})"
        )

    def evaluate(
        self,
        decisive_p: str,
        near_miss_n1: str,
        canary_c: str,
        declared_artifacts: Sequence[str] = (),
    ) -> EvalReport:
        declared = frozenset(normpath(d) for d in declared_artifacts)

        # 1. Necessity: the base agent must not solve P without the skill.
        p_no = self._run(decisive_p, with_skill=False)
        solves_p = p_no.exit_code == 0 and p_no.artifact is not None
        necessity = not solves_p

        # Canary baseline immediately before P-with-skill, so c_post vs c_pre
        # isolates exactly what the skill run did to the context.
        c_pre = self._run(canary_c, with_skill=False)

        # 2. Benefit: with the skill, P must produce a valid artifact.
        p_yes = self._run(decisive_p, with_skill=True)
        benefit = p_yes.exit_code == 0 and p_yes.artifact is not None

        # 3. Invariance: no undeclared pollution, C unchanged after P, and the
        #    decisive artifact is attributable to this run (append on H0).
        #    c_post runs immediately after p_yes (before the near-miss), so it
        #    reflects P's context poisoning specifically.
        unexpected = tuple(d for d in p_yes.workspace_diff if normpath(d) not in declared)
        c_post = self._run(canary_c, with_skill=False)
        canary_stable = (
            c_pre.exit_code == c_post.exit_code
            and c_pre.artifact == c_post.artifact
            and c_pre.workspace_diff == c_post.workspace_diff
        )
        attribution_ok, attribution_desc = self._check_attribution(p_yes)
        invariance = (not unexpected) and canary_stable and attribution_ok

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

        # Invariance detail reports each sub-clause so a failure is localisable.
        inv_parts: list[str] = []
        inv_parts.append("workspace clean modulo declared artifacts" if not unexpected else f"undeclared diff={list(unexpected)}")
        if canary_stable:
            inv_parts.append("canary unchanged")
        else:
            inv_parts.append(
                f"canary pre={c_pre.exit_code}/{c_pre.artifact!r}/{c_pre.workspace_diff} "
                f"post={c_post.exit_code}/{c_post.artifact!r}/{c_post.workspace_diff}"
            )
        inv_parts.append(attribution_desc)
        invariance_detail = "; ".join(inv_parts)

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
                CheckOutcome("invariance", invariance, invariance_detail),
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
