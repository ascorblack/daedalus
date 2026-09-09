"""Skill evaluation harness — the four-check minimal validation loop.

A skill earns registration only if it passes all four checks in one evaluation:

1. **Necessity** — the base agent, WITHOUT the skill, does not solve any case
   of the decisive battery. "Solves a case" is the benefit predicate (exit 0
   with an artifact matching the case's expected value), so necessity is its
   negation: if the base model already produces the expected artifact, the
   skill is redundant — a placebo that only parasitises the base model's own
   ability.
2. **Benefit** — WITH the skill, every case of the decisive battery yields its
   expected artifact (exit 0 and an artifact matching the case's expected
   value, not merely a "loaded" status). The battery is a set of diverse
   decisive inputs, each with its expected artifact. A single input cannot
   rule out a *constant-output* mutant: a skill that ignores its input and
   returns the memorised answer for one input passes every check on that input
   while its rule is inert. A battery of cases with *differing* expected
   artifacts rules that out — the constant mutant fails on any case whose
   expected artifact differs from the one it memorised. (A lookup-table mutant
   that memorises every battery input still passes a closed battery; held-out
   cases would rule it out, which this API does not provide.)
3. **Invariance** — three sub-clauses, all must hold across the battery:
   a. *No undeclared pollution*: after every case of the battery, the union of
      the workspace diffs, minus the skill's *declared* artifacts, is empty.
   b. *Canary stability*: a neutral canary task ``C`` is unchanged before vs
      after the battery (exit code, artifact and workspace diff all match). The
      canary baseline is captured immediately before the battery and the canary
      is re-run immediately after, so the comparison isolates the battery's net
      effect on the context. (The canary is battery-scoped: a case that poisons
      and a later case that unpoisons cancel in the net; per-case isolation is
      not claimed.)
   c. *Append attribution*: each decisive artifact is attributable to its own
      run. The runner reports the artifact target's ``pre_state`` and
      ``post_state`` bytes; the harness checks ``post_state == pre_state + frame``
      (exact append), or ``post_state == frame`` when the target did not
      pre-exist (``pre_state is None``). Keyed on ``H0 = SHA256(pre_state)``.
      This rules out replay, reordering and pre-existing duplicates: a skill
      that merely reads an artifact that was already there produces no append,
      so the check fails. A failed compare-and-swap on ``H0`` (a concurrent
      writer changed the pre-state between snapshot and write) fails the run —
      the race is part of the verification, not a side channel. An empty frame
      on a pre-existing target (a read-only replay) also fails. When the runner
      does not meter ``pre_state``/``post_state`` (``state_metered`` is ``False``
      — the CI mock and legacy runners), this sub-clause is skipped for that
      case, exactly as the cost clause is skipped when the baseline is
      unmetered.
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
from collections.abc import Awaitable, Callable, Sequence
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


def _matches(artifact: object | None, expected: object) -> bool:
    """Whether a produced artifact satisfies a case's expectation.

    ``expected`` is either a fixed value (the artifact must equal it exactly)
    or a validator callable (``expected(artifact)`` is truthy). A ``None``
    artifact never satisfies any expectation. A fixed value is the right
    expectation for deterministic skills; a validator is the right expectation
    for a skill whose output has valid variety (e.g. a design skill that may
    emit any well-formed page) — in that case the validator encodes the shape
    the skill is trusted to produce, which a mere ``is not None`` check does
    not.

    Two footguns, both handled here:

    * A *type* is callable, so ``expected=str`` is treated as a validator
      (``bool(str(artifact))``), NOT as "the artifact equals the class ``str``".
      Pass a lambda for a type check (``lambda a: isinstance(a, str)``); do not
      pass a bare type as a value to compare against.
    * A validator that *raises* fails the match (the case is not solved)
      rather than letting the exception escape ``evaluate`` — a malformed
      artifact that the validator cannot parse is a benefit failure, not a
      harness crash.
    """
    if artifact is None:
        return False
    if callable(expected):
        try:
            return bool(expected(artifact))
        except Exception:
            return False
    return artifact == expected


def _solves(result: RunResult, expected: object) -> bool:
    """The benefit predicate for one case: exit 0 with an artifact that
    matches the case's expectation."""
    return result.exit_code == 0 and _matches(result.artifact, expected)


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
        decisive_cases: Sequence[tuple[str, object]],
        near_miss_n1: str,
        canary_c: str,
        declared_artifacts: Sequence[str] = (),
    ) -> EvalReport:
        declared = frozenset(normpath(d) for d in declared_artifacts)
        cases = list(decisive_cases)
        if not cases:
            # An empty battery would make necessity, benefit and attribution
            # vacuously True (all([]) is True) and register a skill that was
            # never actually tested. Fail closed.
            raise ValueError("decisive_cases must be non-empty: an empty battery vacuously passes the four checks")

        # 1. Necessity: the base agent must not solve ANY case without the skill.
        p_no = [self._run(inp, with_skill=False) for inp, _exp in cases]
        necessity = all(not _solves(r, exp) for r, (_inp, exp) in zip(p_no, cases, strict=True))

        # Canary baseline immediately before the battery, so c_post vs c_pre
        # isolates exactly what the skill runs did to the context.
        c_pre = self._run(canary_c, with_skill=False)

        # 2. Benefit: with the skill, every case must yield its expected artifact.
        p_yes = [self._run(inp, with_skill=True) for inp, _exp in cases]
        benefit = all(_solves(r, exp) for r, (_inp, exp) in zip(p_yes, cases, strict=True))

        # 3. Invariance: no undeclared pollution (across every case), C unchanged
        #    after the battery, and each decisive artifact is attributable to its
        #    run (append on H0). c_post runs immediately after the battery (before
        #    the near-miss), so it reflects the battery's context poisoning.
        unexpected = tuple(d for r in p_yes for d in r.workspace_diff if normpath(d) not in declared)
        c_post = self._run(canary_c, with_skill=False)
        canary_stable = (
            c_pre.exit_code == c_post.exit_code
            and c_pre.artifact == c_post.artifact
            and c_pre.workspace_diff == c_post.workspace_diff
        )
        attributions = [self._check_attribution(r) for r in p_yes]
        attribution_ok = all(ok for ok, _desc in attributions)
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
        inv_parts.append("attribution: " + " | ".join(desc for _ok, desc in attributions))
        invariance_detail = "; ".join(inv_parts)

        return EvalReport(
            outcomes=[
                CheckOutcome(
                    "necessity",
                    necessity,
                    "base agent does not solve any decisive case without the skill"
                    if necessity
                    else "base agent already solves a decisive case — skill is redundant",
                ),
                CheckOutcome(
                    "benefit",
                    benefit,
                    "every decisive case yields its expected artifact with the skill"
                    if benefit
                    else "a decisive case failed or produced the wrong artifact",
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


@dataclass(frozen=True)
class GatedResult:
    """Outcome of one gated registration attempt.

    ``report`` is the four-check :class:`EvalReport`; ``registered`` is True
    only when every check passed and the caller's ``register`` action ran.
    """

    report: EvalReport
    registered: bool


class GatedSkillRegistration:
    """Applies the four-check policy at the registration boundary.

    A skill earns registration only if all four checks pass in a single run.
    The helper is store-agnostic: it takes an :class:`AgentRunner` (the host
    supplies the real runner; CI supplies a deterministic mock) and an async
    ``action`` callable that performs the actual registration (e.g. the
    store's ``create``, closed over the skill identity). ``action`` is
    invoked only when the harness passes; a failing check blocks registration
    and the :class:`GatedResult` carries the report so the caller can surface
    which check failed. If ``action`` itself raises, the exception
    propagates — that is a registration error, not a gate failure.
    """

    def __init__(self, runner: AgentRunner, *, cost_allowance: float = 0.15, cost_floor: float = 0.0) -> None:
        self._harness = SkillEvalHarness(runner, cost_allowance=cost_allowance, cost_floor=cost_floor)

    async def register(
        self,
        action: Callable[[], Awaitable[None]],
        decisive_cases: Sequence[tuple[str, object]],
        near_miss_n1: str,
        canary_c: str,
        declared_artifacts: Sequence[str] = (),
    ) -> GatedResult:
        report = self._harness.evaluate(decisive_cases, near_miss_n1, canary_c, declared_artifacts)
        if not report.passed:
            return GatedResult(report=report, registered=False)
        await action()
        return GatedResult(report=report, registered=True)


__all__ = ["AgentRunner", "CheckOutcome", "EvalReport", "GatedResult", "GatedSkillRegistration", "RunResult", "SkillEvalHarness"]
