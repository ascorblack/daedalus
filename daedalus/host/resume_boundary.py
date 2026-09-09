"""Resume boundary on top of SessionStore.

A finished step is only finished if the next session can name:
1. the latest confirmed completed goal (not the whole plan), and
2. the one unresolved external fact that could invalidate that completion.

This module stores that pair as owned records with an explicit transition
record that refs both. Existence of an artifact alone is not enough.

Each ``put`` is fail-before-write (SessionStore clause 4). This adapter is
not a multi-record transaction: ownership of all three ids is checked before
any write so a rejected call cannot leave a new session-owned leaf. Leaves
are written first so transition refs close (clause 3). While the transition
stands, leaf delete is rejected (clause 5). The transition payload is the
canonical source for the two lines; leaf records are existence anchors.
"""

from __future__ import annotations

from dataclasses import dataclass

from daedalus.host.session_store import Record, SessionStore, SessionStoreError

GOAL_ID = "resume:goal"
UNRESOLVED_ID = "resume:unresolved"
TRANSITION_ID = "resume:transition"


@dataclass(frozen=True)
class ResumeState:
    """What the next session must be able to restate before new work."""

    completed_goal: str
    unresolved_external: str
    transition_id: str = TRANSITION_ID


class ResumeBoundary:
    """Thin adapter: write and read a resume contract via SessionStore."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def _require_session_writable(self, record_id: str) -> None:
        rec = self._store.get(record_id)
        if rec is not None and rec.owner != self._store.session:
            raise SessionStoreError(
                f"update forbidden: {record_id!r} owned by {rec.owner!r}, "
                f"not session {self._store.session!r}"
            )

    def _session_record(self, record_id: str) -> Record | None:
        rec = self._store.get(record_id)
        if rec is None or rec.owner != self._store.session:
            return None
        return rec

    def mark_done(self, completed_goal: str, unresolved_external: str) -> ResumeState:
        """Write goal + unresolved fact, then a transition that refs both.

        Ownership of all three ids is checked before any write. Each put is
        fail-before-write; this adapter is not a multi-record transaction.
        """
        if not isinstance(completed_goal, str) or not completed_goal.strip():
            raise SessionStoreError("completed_goal must be a non-empty str")
        if not isinstance(unresolved_external, str) or not unresolved_external.strip():
            raise SessionStoreError("unresolved_external must be a non-empty str")

        goal = completed_goal.strip()
        unresolved = unresolved_external.strip()

        for rid in (GOAL_ID, UNRESOLVED_ID, TRANSITION_ID):
            self._require_session_writable(rid)

        self._store.put(GOAL_ID, {"kind": "completed_goal", "text": goal})
        self._store.put(
            UNRESOLVED_ID, {"kind": "unresolved_external", "text": unresolved}
        )
        self._store.put(
            TRANSITION_ID,
            {
                "kind": "resume_transition",
                "refs": [GOAL_ID, UNRESOLVED_ID],
                "completed_goal": goal,
                "unresolved_external": unresolved,
            },
        )
        return ResumeState(completed_goal=goal, unresolved_external=unresolved)

    def read(self) -> ResumeState | None:
        """Return the committed resume contract, or None if incomplete.

        Only session-owned records count. The transition payload holds the
        two lines; both leaf records must still exist under this session.
        """
        rec = self._session_record(TRANSITION_ID)
        if rec is None:
            return None
        payload = rec.payload
        goal = payload.get("completed_goal")
        unresolved = payload.get("unresolved_external")
        if not isinstance(goal, str) or not goal.strip():
            return None
        if not isinstance(unresolved, str) or not unresolved.strip():
            return None
        if (
            self._session_record(GOAL_ID) is None
            or self._session_record(UNRESOLVED_ID) is None
        ):
            return None
        return ResumeState(
            completed_goal=goal.strip(),
            unresolved_external=unresolved.strip(),
        )

    def can_resume(self) -> bool:
        """True only when both boundary lines are restatable from the store."""
        return self.read() is not None

    def clear(self) -> None:
        """Drop the transition first, then the leaves (inbound-ref order).

        Foreign-owned records under these ids are rejected, not skipped.
        Like ``mark_done``, this is not a multi-record transaction: if a
        later id is foreign, earlier deletes in this call already committed.
        """
        for rid in (TRANSITION_ID, GOAL_ID, UNRESOLVED_ID):
            rec = self._store.get(rid)
            if rec is None:
                continue
            if rec.owner != self._store.session:
                raise SessionStoreError(
                    f"delete forbidden: {rid!r} owned by {rec.owner!r}"
                )
            self._store.delete(rid)
