"""Skill evaluation harness: the four-check minimal validation loop."""

from __future__ import annotations

import pytest

from daedalus.host.skill_eval import GatedSkillRegistration, RunResult, SkillEvalHarness

# Outcomes are reported in execution order: necessity, benefit, invariance, selectivity.
NEC, BEN, INV, SEL = 0, 1, 2, 3


def ok(exit_code: int = 0, artifact: object = None, invoked: bool = False, cost: float = 0.0, diff=(),
       pre_state: bytes | None = None, post_state: bytes | None = None, cas_failed: bool = False,
       state_metered: bool = False) -> RunResult:
    return RunResult(exit_code, artifact, invoked, cost, tuple(diff), pre_state, post_state, cas_failed, state_metered)


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
    runner = ScriptedRunner(passing_table())
    h = SkillEvalHarness(runner)
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.passed
    assert rep.summary().startswith("PASS")
    assert rep.failed() == []
    # Lock the run order: c_pre immediately before P-with-skill, c_post
    # immediately after it, and the near-miss last.
    assert runner.calls == [
        ("P", False),      # necessity
        ("canary", False), # c_pre
        ("P", True),       # benefit
        ("canary", False), # c_post
        ("N1", False),     # baseline
        ("N1", True),      # selectivity
    ]


def test_necessity_fails_when_base_solves_p() -> None:
    table = passing_table()
    table[("P", False)] = ok(0, artifact="A")  # base model already solves P
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["necessity"]
    assert "redundant" in rep.outcomes[NEC].detail


def test_necessity_passes_when_base_exits_0_but_no_artifact() -> None:
    table = passing_table()
    table[("P", False)] = ok(0, artifact=None)  # exit 0 but no valid artifact: not a solution
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert "necessity" not in rep.failed()


def test_benefit_fails_when_no_artifact() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact=None, invoked=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary")
    assert rep.failed() == ["benefit"]


def test_benefit_fails_when_p_errors() -> None:
    table = passing_table()
    table[("P", True)] = ok(1, artifact=None, invoked=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary")
    assert rep.failed() == ["benefit"]


def test_selectivity_fails_when_n1_triggered() -> None:
    table = passing_table()
    table[("N1", True)] = ok(0, invoked=True, cost=11.0)  # triggered, but cheap
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["selectivity"]
    assert "triggered" in rep.outcomes[SEL].detail  # message blames the trigger, not the cost


def test_selectivity_fails_when_n1_too_expensive() -> None:
    table = passing_table()
    table[("N1", True)] = ok(0, invoked=False, cost=20.0)  # quiet but 2x baseline
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["selectivity"]
    assert "over budget" in rep.outcomes[SEL].detail


def test_cost_clause_skipped_when_baseline_unmetered() -> None:
    table = passing_table()
    table[("N1", False)] = ok(0, cost=0.0)  # baseline not metered
    table[("N1", True)] = ok(0, invoked=False, cost=5.0)  # positive cost, clause skipped
    h = SkillEvalHarness(ScriptedRunner(table))  # default floor
    rep = h.evaluate([("P", "A")], "N1", "canary")
    assert "selectivity" not in rep.failed()
    assert "unmetered" in rep.outcomes[SEL].detail


def test_cost_floor_guards_tiny_baseline() -> None:
    table = passing_table()
    table[("N1", False)] = ok(0, cost=0.1)  # tiny baseline: 15% headroom is noise
    table[("N1", True)] = ok(0, invoked=False, cost=0.2)  # over 0.115, within floor 0.5
    h = SkillEvalHarness(ScriptedRunner(table), cost_floor=0.5)
    rep = h.evaluate([("P", "A")], "N1", "canary")
    assert "selectivity" not in rep.failed()


def test_invariance_fails_on_undeclared_diff() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact="A", invoked=True, diff=("out/index.html", "rogue.tmp"))
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "rogue.tmp" in rep.outcomes[INV].detail


def test_invariance_passes_with_path_normalized_declared_artifact() -> None:
    table = passing_table()
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["./out/index.html"])
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
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
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
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]


def test_summary_marks_fail() -> None:
    table = passing_table()
    table[("P", True)] = ok(1, artifact=None, invoked=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary")
    assert not rep.passed
    assert rep.summary().startswith("FAIL")
    assert "benefit=FAIL" in rep.summary()


# --- Invariance sub-clause c: append attribution (H0 + exact append-frame) ---


def _attr_table(pre_state: bytes | None, post_state: bytes | None, **kw) -> dict:
    """passing_table with the P-with-skill run metering byte-level state."""
    table = passing_table()
    table[("P", True)] = ok(0, artifact="A", invoked=True, diff=("out/index.html",),
                            pre_state=pre_state, post_state=post_state, state_metered=True, **kw)
    return table


def test_attribution_passes_pure_creation() -> None:
    # Target did not pre-exist: post must be exactly the frame.
    table = _attr_table(pre_state=None, post_state=b"A")
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.passed
    assert "create verified" in rep.outcomes[INV].detail


def test_attribution_passes_exact_append() -> None:
    # Target pre-existed: post must be exactly pre + frame, keyed on H0.
    table = _attr_table(pre_state=b"existing", post_state=b"existingA")
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.passed
    assert "append verified" in rep.outcomes[INV].detail
    assert "H0=" in rep.outcomes[INV].detail


def test_attribution_fails_on_replay_pre_existing() -> None:
    # The hole the H0 binding closes: the artifact pre-existed and the run
    # appended nothing (post == pre). A diff-only or "artifact is not None"
    # check would pass this; the exact append-frame check must fail it.
    table = _attr_table(pre_state=b"A", post_state=b"A")
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "append mismatch" in rep.outcomes[INV].detail


def test_attribution_fails_on_wrong_frame() -> None:
    # Target did not pre-exist but the run wrote something other than the frame.
    table = _attr_table(pre_state=None, post_state=b"wrong")
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "create mismatch" in rep.outcomes[INV].detail


def test_attribution_fails_on_cas() -> None:
    # A concurrent writer changed the pre-state between snapshot and write:
    # even if post matched, the lost CAS makes the run invalid.
    table = _attr_table(pre_state=b"existing", post_state=b"existingA", cas_failed=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "CAS failed" in rep.outcomes[INV].detail


def test_attribution_skipped_when_unmetered() -> None:
    # Legacy/CI mock: no byte-level state metered -> sub-clause skipped, passes.
    h = SkillEvalHarness(ScriptedRunner(passing_table()))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.passed
    assert "attribution unmetered" in rep.outcomes[INV].detail


def test_attribution_fails_when_frame_not_bytes() -> None:
    # State was metered but the artifact is not a byte frame: cannot verify.
    table = passing_table()
    table[("P", True)] = ok(0, artifact={"k": 1}, invoked=True, diff=("out/index.html",),
                            pre_state=None, post_state=b"something", state_metered=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    # Expected artifact is the (non-byte) dict itself, so benefit passes and
    # only the attribution sub-clause (cannot form a byte frame) fails.
    rep = h.evaluate([("P", {"k": 1})], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "cannot form byte frame" in rep.outcomes[INV].detail


def test_attribution_fails_on_empty_frame_replay() -> None:
    # The remaining replay hole: the skill reads a pre-existing target and
    # reports an empty artifact (frame b""). A plain append would be
    # pre + b"" == pre, which matches post — the empty-frame guard fails it.
    table = passing_table()
    table[("P", True)] = ok(0, artifact="", invoked=True, diff=("out/index.html",),
                            pre_state=b"A", post_state=b"A", state_metered=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    # Expected artifact is the empty string, so benefit passes and only the
    # empty-frame attribution guard fails.
    rep = h.evaluate([("P", "")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "empty frame" in rep.outcomes[INV].detail


def test_attribution_fails_on_metered_create_no_write() -> None:
    # Metered run, target absent before and after, but an artifact was claimed:
    # a failed create must not be skipped as unmetered.
    table = passing_table()
    table[("P", True)] = ok(0, artifact="A", invoked=True, diff=("out/index.html",),
                            pre_state=None, post_state=None, state_metered=True)
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert rep.failed() == ["invariance"]
    assert "create mismatch" in rep.outcomes[INV].detail


# --- Gated registration: the four-check policy at the registration boundary ---


class RecordingRegister:
    """An async register action that records that it was invoked."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


async def test_gated_registration_registers_when_all_pass() -> None:
    action = RecordingRegister()
    gate = GatedSkillRegistration(ScriptedRunner(passing_table()))
    result = await gate.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert result.registered
    assert result.report.passed
    assert action.calls == 1


async def test_gated_registration_blocks_when_necessity_fails() -> None:
    table = passing_table()
    table[("P", False)] = ok(0, artifact="A")  # base agent already solves P
    action = RecordingRegister()
    gate = GatedSkillRegistration(ScriptedRunner(table))
    result = await gate.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert not result.registered
    assert result.report.failed() == ["necessity"]
    assert action.calls == 0


async def test_gated_registration_blocks_when_benefit_fails() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact=None, invoked=True)  # no valid artifact
    action = RecordingRegister()
    gate = GatedSkillRegistration(ScriptedRunner(table))
    result = await gate.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert not result.registered
    assert result.report.failed() == ["benefit"]
    assert action.calls == 0


async def test_gated_registration_blocks_when_selectivity_fails() -> None:
    table = passing_table()
    table[("N1", True)] = ok(0, invoked=True, cost=11.0)  # triggered on a near-miss
    action = RecordingRegister()
    gate = GatedSkillRegistration(ScriptedRunner(table))
    result = await gate.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert not result.registered
    assert result.report.failed() == ["selectivity"]
    assert action.calls == 0


async def test_gated_registration_blocks_when_invariance_fails() -> None:
    table = passing_table()
    table[("P", True)] = ok(0, artifact="A", invoked=True, diff=("out/index.html", "rogue.tmp"))
    action = RecordingRegister()
    gate = GatedSkillRegistration(ScriptedRunner(table))
    result = await gate.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert not result.registered
    assert result.report.failed() == ["invariance"]
    assert action.calls == 0


async def test_gated_registration_blocks_on_attribution_replay() -> None:
    # The H0 binding: a skill that merely reads a pre-existing artifact
    # (no append) must not be registered.
    table = _attr_table(pre_state=b"A", post_state=b"A")
    action = RecordingRegister()
    gate = GatedSkillRegistration(ScriptedRunner(table))
    result = await gate.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert not result.registered
    assert result.report.failed() == ["invariance"]
    assert action.calls == 0


async def test_gated_registration_propagates_register_error() -> None:
    # A store failure is a registration error, not a gate failure: it must
    # propagate, not be swallowed into registered=False.
    async def broken() -> None:
        raise RuntimeError("store down")

    gate = GatedSkillRegistration(ScriptedRunner(passing_table()))
    with pytest.raises(RuntimeError, match="store down"):
        await gate.register(broken, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])


async def test_gated_registration_forwards_cost_allowance() -> None:
    # N1 costs 120% of baseline: over the default 15% allowance, within 50%.
    table = passing_table()
    table[("N1", False)] = ok(0, cost=10.0)
    table[("N1", True)] = ok(0, invoked=False, cost=12.0)
    strict = GatedSkillRegistration(ScriptedRunner(table))
    assert not (await strict.register(RecordingRegister(), [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])).registered
    lenient = GatedSkillRegistration(ScriptedRunner(table), cost_allowance=0.5)
    assert (await lenient.register(RecordingRegister(), [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])).registered


async def test_gated_registration_forwards_cost_floor() -> None:
    # Tiny baseline: the 15% headroom is noise, so a floor of 0.5 admits it.
    table = passing_table()
    table[("N1", False)] = ok(0, cost=0.1)
    table[("N1", True)] = ok(0, invoked=False, cost=0.2)
    strict = GatedSkillRegistration(ScriptedRunner(table))
    assert not (await strict.register(RecordingRegister(), [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])).registered
    floored = GatedSkillRegistration(ScriptedRunner(table), cost_floor=0.5)
    assert (await floored.register(RecordingRegister(), [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])).registered


async def test_gated_registration_wires_into_directory_skill_store(tmp_path) -> None:
    from protocore.contracts.skills import SkillUpsertInput

    from daedalus.host.skills import DirectorySkillStore

    store = DirectorySkillStore(tmp_path / "skills")
    payload = SkillUpsertInput(name="Tide Pool", description="d", body_md="body")
    calls = {"n": 0}

    async def action() -> None:
        calls["n"] += 1
        await store.create("t", payload)

    # A passing skill is registered in the store exactly once.
    gate = GatedSkillRegistration(ScriptedRunner(passing_table()))
    result = await gate.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert result.registered
    assert calls["n"] == 1
    assert [e.id for e in await store.list("t")] == ["tide-pool"]

    # A failing skill is NOT registered: the register action is never invoked.
    table = passing_table()
    table[("P", False)] = ok(0, artifact="A")
    gate2 = GatedSkillRegistration(ScriptedRunner(table))
    result2 = await gate2.register(action, [("P", "A")], "N1", "canary", declared_artifacts=["out/index.html"])
    assert not result2.registered
    assert calls["n"] == 1  # unchanged: the failing attempt never called register
    assert [e.id for e in await store.list("t")] == ["tide-pool"]


# --- The battery closes the two blind spots named in review ---


class HardcodedMutant:
    """A skill that ignores its input and returns the memorised answer for the
    primary decisive input. On a single-case battery it is indistinguishable
    from the real skill; on a multi-case battery it fails benefit on any case
    whose expected artifact differs from the memorised one."""

    def __init__(self, memorised: object) -> None:
        self.memorised = memorised

    def __call__(self, task: str, *, with_skill: bool) -> RunResult:
        if not with_skill:
            return ok(1, artifact=None)  # base agent cannot solve any case
        if task == "N1":
            return ok(0, artifact=None, invoked=False)  # quiet on the near-miss
        return ok(0, artifact=self.memorised, invoked=True)  # hardcoded for any P


def test_battery_catches_hardcoded_output_mutant() -> None:
    # The falsification fixture: the smallest case where the four checks
    # all pass while the claimed rule is inert is a skill that hardcodes the
    # known answer for the single decisive input.
    mutant = HardcodedMutant("A1")
    h = SkillEvalHarness(mutant)

    # Single-case battery: the mutant is indistinguishable from the real skill.
    single = h.evaluate([("P1", "A1")], "N1", "canary")
    assert single.passed, "a single-case battery cannot rule out a hardcoded-output mutant"

    # Multi-case battery: the mutant fails benefit on P2 (expected A2, got A1).
    multi = h.evaluate([("P1", "A1"), ("P2", "A2")], "N1", "canary")
    assert multi.failed() == ["benefit"]
    assert "wrong artifact" in multi.outcomes[BEN].detail


def test_expected_artifact_catches_malformed_append() -> None:
    # kolpaq's symmetric false-negative: a skill that appends a malformed line
    # to a shared log produces a non-None artifact, so the old benefit
    # predicate (artifact is not None) passed it. The expected-artifact
    # benefit fails it. (The battery is irrelevant here — a single case with an
    # exact expectation already closes this hole; the battery is what closes
    # the constant-output hole in the test above.)
    table = {
        ("canary", False): ok(0, artifact="C"),
        ("canary", True): ok(0, artifact="C"),
        ("P", False): ok(1, artifact=None),
        # Appends a malformed line: non-None, so the old predicate passed.
        ("P", True): ok(0, artifact="MALFORMED", invoked=True, diff=("log.txt",)),
        ("N1", False): ok(0, cost=10.0),
        ("N1", True): ok(0, invoked=False, cost=11.0),
    }
    h = SkillEvalHarness(ScriptedRunner(table))
    # Expected artifact is the correct line, not "any non-None".
    rep = h.evaluate([("P", "correct line")], "N1", "canary", declared_artifacts=["log.txt"])
    assert rep.failed() == ["benefit"]
    assert "wrong artifact" in rep.outcomes[BEN].detail


def test_empty_battery_rejected() -> None:
    # An empty battery would make necessity/benefit/attribution vacuously True
    # (all([]) is True) and register a skill that was never tested. Fail closed.
    h = SkillEvalHarness(ScriptedRunner(passing_table()))
    with pytest.raises(ValueError, match="non-empty"):
        h.evaluate([], "N1", "canary")


def test_necessity_fails_when_base_solves_only_second_case() -> None:
    # The base agent solves the SECOND case (not the first): necessity must
    # still fail (base solves ANY case).
    table = {
        ("canary", False): ok(0, artifact="C"),
        ("canary", True): ok(0, artifact="C"),
        ("P1", False): ok(1, artifact=None),          # base does NOT solve P1
        ("P1", True): ok(0, artifact="A1", invoked=True),
        ("P2", False): ok(0, artifact="A2"),          # base DOES solve P2
        ("P2", True): ok(0, artifact="A2", invoked=True),
        ("N1", False): ok(0, cost=10.0),
        ("N1", True): ok(0, invoked=False, cost=11.0),
    }
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P1", "A1"), ("P2", "A2")], "N1", "canary")
    assert rep.failed() == ["necessity"]


def test_invariance_fails_on_undeclared_diff_in_later_case() -> None:
    # An undeclared diff appears only in the SECOND case: invariance must fail
    # (the pollution check is the union across all cases).
    table = {
        ("canary", False): ok(0, artifact="C"),
        ("canary", True): ok(0, artifact="C"),
        ("P1", False): ok(1, artifact=None),
        ("P1", True): ok(0, artifact="A1", invoked=True, diff=("out/1.html",)),
        ("P2", False): ok(1, artifact=None),
        # P2 writes an undeclared file: the union catches it.
        ("P2", True): ok(0, artifact="A2", invoked=True, diff=("out/2.html", "rogue.tmp")),
        ("N1", False): ok(0, cost=10.0),
        ("N1", True): ok(0, invoked=False, cost=11.0),
    }
    h = SkillEvalHarness(ScriptedRunner(table))
    # Only out/1.html and out/2.html are declared; rogue.tmp is not.
    rep = h.evaluate([("P1", "A1"), ("P2", "A2")], "N1", "canary", declared_artifacts=["out/1.html", "out/2.html"])
    assert rep.failed() == ["invariance"]
    assert "rogue.tmp" in rep.outcomes[INV].detail


def test_validator_accepts_wellformed_rejects_malformed() -> None:
    # A validator callable encodes the shape the skill is trusted to produce.
    # It accepts a well-formed artifact and rejects a malformed one.
    def well_formed(a):
        return isinstance(a, str) and a.startswith("line:")

    table = {
        ("canary", False): ok(0, artifact="C"),
        ("canary", True): ok(0, artifact="C"),
        ("P", False): ok(1, artifact=None),
        ("P", True): ok(0, artifact="line: ok", invoked=True, diff=("log.txt",)),
        ("N1", False): ok(0, cost=10.0),
        ("N1", True): ok(0, invoked=False, cost=11.0),
    }
    h = SkillEvalHarness(ScriptedRunner(table))
    # Well-formed artifact matches the validator: benefit passes.
    rep = h.evaluate([("P", well_formed)], "N1", "canary", declared_artifacts=["log.txt"])
    assert "benefit" not in rep.failed()

    # Malformed artifact (no "line:" prefix) fails the validator: benefit fails.
    table[("P", True)] = ok(0, artifact="garbage", invoked=True, diff=("log.txt",))
    rep2 = h.evaluate([("P", well_formed)], "N1", "canary", declared_artifacts=["log.txt"])
    assert rep2.failed() == ["benefit"]


def test_validator_that_raises_fails_closed() -> None:
    # A validator that raises on the artifact fails the match (benefit fails),
    # it does not crash evaluate.
    def raising(a):
        raise ValueError("cannot parse")

    table = {
        ("canary", False): ok(0, artifact="C"),
        ("canary", True): ok(0, artifact="C"),
        ("P", False): ok(1, artifact=None),
        ("P", True): ok(0, artifact="whatever", invoked=True, diff=("out.html",)),
        ("N1", False): ok(0, cost=10.0),
        ("N1", True): ok(0, invoked=False, cost=11.0),
    }
    h = SkillEvalHarness(ScriptedRunner(table))
    rep = h.evaluate([("P", raising)], "N1", "canary", declared_artifacts=["out.html"])
    assert rep.failed() == ["benefit"]


def test_constant_mutant_with_same_expected_artifact_still_passes() -> None:
    # Documents the limit: a constant-output mutant that happens to return the
    # SAME expected artifact for every case in the battery still passes. The
    # battery only rules out a constant mutant when the cases have DIFFERING
    # expected artifacts. (A lookup-table mutant that memorises every battery
    # input passes regardless; held-out cases would be needed to rule it out.)
    mutant = HardcodedMutant("A1")
    h = SkillEvalHarness(mutant)
    rep = h.evaluate([("P1", "A1"), ("P2", "A1")], "N1", "canary")  # both expect A1
    assert rep.passed
