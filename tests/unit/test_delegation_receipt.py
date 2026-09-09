"""Delegation receipts: the assigned_target/claimed_target split and fail-before-write.

The laundering hole: a single ``target`` field lets a child fill it with an invented
locator, write to it, and report a shape-valid PASS. The split makes
``assigned_target`` parent-only and ``claimed_target`` child-only, so a filled
``claimed_target`` cannot populate ``assigned_target``.
"""

from __future__ import annotations

from daedalus.tools.delegation_receipt import (
    DelegationReceipt,
    evaluate,
    should_reject_write,
)

BASE = dict(
    criterion="write landed",
    command="curl -X PUT …",
    exit_code=0,
    output_digest="ab12",
    at="2026-09-09T07:00:00Z",
)


def receipt(**kw) -> DelegationReceipt:
    full = dict(BASE)
    full.update(kw)
    return DelegationReceipt(**full)


def test_should_reject_write_canary():
    assert should_reject_write(None) is True
    assert should_reject_write("") is True
    assert should_reject_write("   ") is True
    assert should_reject_write("https://x.com/record/1") is False


def test_fail_before_write_no_write_is_unknown():
    # The laundering case: parent supplies no locator; child emits PASS with a filled
    # claimed_target; parent must have zero write and UNKNOWN.
    r = receipt(assigned_target=None, claimed_target="https://invented.example/1",
                write_occurred=False)
    assert evaluate(r) == "UNKNOWN"
    # The child's filled claimed_target did NOT populate assigned_target.
    assert r.assigned_target is None


def test_fail_before_write_with_write_is_laundering_fail():
    # A write occurred without a pinned target: the canary failed. The child invented
    # a locator and wrote to it — a later re-read of that locator is laundering.
    r = receipt(assigned_target=None, claimed_target="https://invented.example/1",
                write_occurred=True)
    assert evaluate(r) == "FAIL"


def test_pass_pinned_and_matching():
    r = receipt(assigned_target="https://x.com/record/1",
                claimed_target="https://x.com/record/1", write_occurred=True)
    assert evaluate(r) == "PASS"


def test_fail_drift_child_wrote_elsewhere():
    # Pinned target, but the child wrote to a different one.
    r = receipt(assigned_target="https://x.com/record/1",
                claimed_target="https://x.com/record/2", write_occurred=True)
    assert evaluate(r) == "FAIL"


def test_fail_no_write():
    # Pinned target but nothing was written (the claim of success is false).
    r = receipt(assigned_target="https://x.com/record/1",
                claimed_target="https://x.com/record/1", write_occurred=False)
    assert evaluate(r) == "FAIL"


def test_fail_nonzero_exit():
    r = receipt(assigned_target="https://x.com/record/1",
                claimed_target="https://x.com/record/1", write_occurred=True,
                exit_code=1)
    assert evaluate(r) == "FAIL"


def test_whitespace_normalized_comparison():
    # A trailing space in the child's claimed target is formatting, not laundering.
    r = receipt(assigned_target="https://x.com/record/1",
                claimed_target="https://x.com/record/1 ", write_occurred=True)
    assert evaluate(r) == "PASS"


def test_whitespace_only_assigned_target_is_empty():
    # A whitespace-only assigned_target is treated as empty (no pinned target).
    assert evaluate(receipt(assigned_target="   ", claimed_target="https://x/1",
                            write_occurred=False)) == "UNKNOWN"
    assert evaluate(receipt(assigned_target="   ", claimed_target="https://x/1",
                            write_occurred=True)) == "FAIL"


def test_fail_pinned_but_child_claimed_nothing():
    # Pinned target, a write happened, but the child wrote no claimed target.
    assert evaluate(receipt(assigned_target="https://x.com/record/1",
                            claimed_target=None, write_occurred=True)) == "FAIL"
    assert evaluate(receipt(assigned_target="https://x.com/record/1",
                            claimed_target="", write_occurred=True)) == "FAIL"


def test_child_cannot_populate_assigned_target():
    # The laundering hole in a single-target field: a child that only controls the
    # receipt can fill claimed_target, but assigned_target stays parent-only.
    r = receipt(assigned_target=None, claimed_target="https://x.com/record/1",
                write_occurred=True)
    assert r.assigned_target is None
    assert evaluate(r) == "FAIL"  # a write without a pinned target is laundering
