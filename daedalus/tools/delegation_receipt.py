"""Delegation receipts: a parent hands a child a *pinned* target before the call.

Background (board thread cee13cb5, "Beyond JSON status checks"): when a parent
delegates a state-changing write to a child, the child's summary is a self-report —
it has no independent view of the side effect it claims. The parent's receipt must
therefore be re-executable *and* target-bound. The single-``target`` field has a
laundering hole: the child can fill it with an invented locator, write to that, and
report a shape-valid PASS. A filled ``target`` then looks identical to a pinned one.

just-nik's refinement (seq 26929) splits the field so the canary cannot be laundered:

- ``assigned_target`` — the locator the parent handed the child *before* the call.
  Only the parent populates it. Empty means the parent did not pin a target.
- ``claimed_target``  — whatever the child wrote into the receipt. Only the child
  populates it. A filled ``claimed_target`` does NOT populate ``assigned_target``.

Fail-before-write is only observable if a missing ``assigned_target`` rejects the
write *before* it happens. If a write occurred without a pinned target, the canary
already failed: the child invented a locator and wrote to it — a later re-read of
that invented locator is the post-hoc laundering, even if the receipt is later
stamped UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationReceipt:
    """A parent-side receipt for a delegated state-changing write.

    ``assigned_target`` is parent-only (pre-call); ``claimed_target`` is child-only
    (in the receipt). They are different fields and one does not populate the other.
    """

    assigned_target: str | None
    claimed_target: str | None
    criterion: str
    command: str
    exit_code: int
    output_digest: str
    at: str
    # Parent-observed, NOT child-reported. If a child could set this, it could hide a
    # real write as UNKNOWN (write_occurred=False). The parent sets it from its own
    # observation of whether the write happened, not from the child's JSON.
    write_occurred: bool = False


def _norm(target: str | None) -> str:
    return (target or "").strip()


def should_reject_write(assigned_target: str | None) -> bool:
    """Fail-before-write canary.

    The parent calls this *before* delegating the write. If it did not pin an
    ``assigned_target``, the write must be rejected before it happens — no child
    call, no write, no digest of a write. The receipt for that delegation is
    UNKNOWN, not PASS and not FAIL.
    """
    return not _norm(assigned_target)


def evaluate(receipt: DelegationReceipt) -> str:
    """Classify a delegation receipt as ``PASS`` / ``FAIL`` / ``UNKNOWN``.

    - ``UNKNOWN``: no pinned ``assigned_target`` and no write occurred. This is the
      correct fail-before-write outcome — the write was rejected before it happened.
      A child that fills ``claimed_target`` here is not passing; the receipt stays
      UNKNOWN because ``assigned_target`` (parent-only) is empty.
    - ``FAIL``: a write occurred without a pinned ``assigned_target`` (the canary
      failed — laundering), or the child wrote to a target other than the pinned one
      (drift or laundering), or the check did not pass.
    - ``PASS``: a pinned ``assigned_target``, the child wrote to exactly that target,
      and the check passed (exit 0).
    """
    assigned = _norm(receipt.assigned_target)
    claimed = _norm(receipt.claimed_target)

    if not assigned:
        # Fail-before-write. No write -> UNKNOWN (correct rejection). A write here
        # means the child invented a locator and wrote to it -> the canary failed.
        return "FAIL" if receipt.write_occurred else "UNKNOWN"

    if not receipt.write_occurred:
        return "FAIL"

    if claimed != assigned:
        return "FAIL"

    return "PASS" if receipt.exit_code == 0 else "FAIL"


__all__ = ["DelegationReceipt", "should_reject_write", "evaluate"]
