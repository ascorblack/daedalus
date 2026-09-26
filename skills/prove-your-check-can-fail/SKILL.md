---
name: prove-your-check-can-fail
description: Use when a test suite, checker, linter or mutation harness must be shown to detect the defect it claims to detect — before trusting a green run, when auditing "is this check live?", when a suite prints all-pass and you need to know what that covers, or when a claim about where a number came from is in question.
---
# Prove your check can fail

A green run is evidence about the checks that ran, and about nothing else. These are the
four procedures that repeatedly found real defects in a ledger of ~180 check entries;
each one is cheap and each one has a measured case behind it.

## 1. Show the checker goes red when the defect is planted

Do not read the check; plant the defect and read the exit code. For each gate, mutate a
COPY (never the live tree):

```sh
git archive HEAD | tar -x -C /tmp/copy          # a copy built from the tree itself
# delete the clause the check is supposed to catch, then:
python3 check.py; echo "exit $?"
```

Two clauses deserve a mutant each: if a check tests parity AND integrality, delete each
one in its own copy. A single mutant that removes both tells you nothing about either.
Real case: a lattice test that checked parity only — a helper with the integrality half
deleted still passed the self-test, so the self-test certified half a comparison.

## 2. Make the harness an item of the run, and grep for its name

A harness outside the run has no colour anyone can see. This is a one-line audit and it
has found a red harness that had been red since it was written:

```sh
grep -rn "selftest.py" repro/run_all.sh        # empty output is the finding
```

Then add it as a standing item, so the next drift is visible.

## 3. Build the fixture tree from the record, not from a list of names

A fixture that copies a hand list of files is a sentence about what the subject looked
like when the list was written. When the subject grows an import the list does not know,
every case in the harness exits non-zero for a missing module — and the ten cases that
assert "the checker refuses X" all pass for a reason unrelated to X, while the single
case that asserts "the clean copy passes" is the only reported failure. The tell is not
the failure: it is that ten green and one red had the same cause.

```python
names = subprocess.run(["git", "ls-files", "-z"], cwd=HERE,
                       capture_output=True, text=True, check=True).stdout.split("\0")
# keep a literal fallback for a tree git cannot answer for (an export, a bare copy)
```

## 4. A fixture must supply the subject's own values in every field the gate reads

A case that plants its own `expected` beside the subject's probe and observed value
measures a comparison that never runs. When the gate compares three fields, one differing
field means no refusal is due, the case exits clean, and the case that asserts a refusal
reads as passing — again for the harness's reason, not the gate's. After repairing a
fixture, re-run the harness: the cases that were masked become visible one at a time.

## The provenance rule, which is not a code check

A claim about WHERE a number came from is settled by running the OLD code, not by reading
the diff. Real case: a print with four format arguments was read as taking its fourth
quantity from the third, and a defect class was registered that had no instance in the
code. Recover the parent revision and call the function:

```sh
git show HEAD~2:probes/probe.py | sed -n '160,180p'
```

Withdraw the class when the instance does not exist; keep the repair if it was useful. A
class registered on a misattribution is a false record whatever sits beside it.

## The revision rule for reports

When a review or a benchmark ships its evidence as a script, make the script read the
TREE (`git archive HEAD`) and put `git rev-parse HEAD` in the report. A script that
extracts a frozen archive reproduces the revision frozen into it; after the repairs land,
re-running it prints the repaired defects as live, under a claim about the tree as it
stands.

## Reporting

Print, per case, what an outsider needs to judge: the mutation applied, the exit code
observed, and the exit code wanted. A table of "CAUGHT / OPEN" per case is what makes a
re-measurement worth reading; a sentence is not.
