"""Session-local store with ownership and reference-closure gates.

Minimal model for a per-host-session map of record IDs to (owner, payload).
Writes that would break isolation are rejected before the store changes
(fail-before-write). Ownership is set by the host, never trusted from the caller.

Five clauses (preservation checklist for a put):

1. Initial invariant I(D): every private reference reachable from a record's
   payload resolves to an existing record owned by the same owner.
2. Authorization: create requires a fresh ID; update requires current owner ==
   authenticated session. The host sets the new owner to the session.
3. Reference closure: every private ref in the proposed payload resolves with
   owner == session, checked against the same snapshot used for commit.
4. Atomic frame: commit changes only the authorized record and stores owner
   with payload together; rejected writes leave D unchanged.
5. Transition closure: deletion rejects records that still have inbound private
   references; recovery exposes only committed states.

This establishes reference isolation under the stated store model, not full
agent noninterference or progress.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class SessionStoreError(ValueError):
    """Rejected mutation; the store was not changed."""


@dataclass(frozen=True)
class Record:
    owner: str
    payload: dict[str, Any]


def _require_id(record_id: object) -> str:
    if not isinstance(record_id, str) or record_id == "":
        raise SessionStoreError("record id must be a non-empty str")
    return record_id


@dataclass
class SessionStore:
    """In-memory session-local store. ``session`` is the authenticated host id.

    ``get`` and ``snapshot`` return deep copies so callers cannot plant refs
    into committed payloads. ``refs`` arrays are JSON lists only (tuples are
    not walked). There is no persistence layer in this minimal model.
    """

    session: str
    _data: dict[str, Record] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Record]:
        return {
            rid: Record(owner=rec.owner, payload=copy.deepcopy(rec.payload))
            for rid, rec in self._data.items()
        }

    def get(self, record_id: str) -> Record | None:
        _require_id(record_id)
        rec = self._data.get(record_id)
        if rec is None:
            return None
        return Record(owner=rec.owner, payload=copy.deepcopy(rec.payload))

    def refs_in(self, payload: dict[str, Any]) -> list[str]:
        """Collect private reference ids from a payload.

        Convention: a value under key ``ref`` or ``refs`` (JSON list) names
        another record id. Nested dicts/lists are walked; tuples are ignored
        by design. Shared immutable objects are out of scope.
        """
        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "ref" in node and isinstance(node["ref"], str):
                    found.append(node["ref"])
                if "refs" in node and isinstance(node["refs"], list):
                    for item in node["refs"]:
                        if isinstance(item, str):
                            found.append(item)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return found

    def check_invariant(self) -> None:
        """Raise if I(D) fails (clause 1)."""
        for rid, rec in self._data.items():
            for ref in self.refs_in(rec.payload):
                target = self._data.get(ref)
                if target is None:
                    raise SessionStoreError(
                        f"invariant: {rid!r} refs missing {ref!r}"
                    )
                if target.owner != rec.owner:
                    raise SessionStoreError(
                        f"invariant: {rid!r} refs {ref!r} owned by "
                        f"{target.owner!r}, not {rec.owner!r}"
                    )

    def put(self, record_id: str, payload: dict[str, Any]) -> None:
        """Create or update one record under the authenticated session.

        Fail-before-write: on any rejection the store is unchanged.
        """
        record_id = _require_id(record_id)
        if not isinstance(payload, dict):
            raise SessionStoreError("payload must be a dict")

        existing = self._data.get(record_id)
        # Clause 2: create = fresh id; update = owner must already be session.
        # Host sets owner to session; never trust a caller-supplied owner field.
        if existing is not None and existing.owner != self.session:
            raise SessionStoreError(
                f"update forbidden: {record_id!r} owned by "
                f"{existing.owner!r}, not session {self.session!r}"
            )

        # Clause 3: reference closure against the commit snapshot.
        snap = self.snapshot()
        for ref in self.refs_in(payload):
            target = snap.get(ref)
            if target is None:
                raise SessionStoreError(
                    f"ref closure: {ref!r} does not exist"
                )
            if target.owner != self.session:
                raise SessionStoreError(
                    f"ref closure: {ref!r} owned by {target.owner!r}, "
                    f"not session {self.session!r}"
                )

        # Clause 4: atomic frame — owner and deep-copied payload together.
        self._data[record_id] = Record(
            owner=self.session, payload=copy.deepcopy(payload)
        )

    def delete(self, record_id: str) -> None:
        """Delete a session-owned record if nothing private still points at it."""
        record_id = _require_id(record_id)
        existing = self._data.get(record_id)
        if existing is None:
            raise SessionStoreError(f"missing {record_id!r}")
        if existing.owner != self.session:
            raise SessionStoreError(
                f"delete forbidden: {record_id!r} owned by {existing.owner!r}"
            )
        # Clause 5: reject if inbound private refs remain.
        for rid, rec in self._data.items():
            if rid == record_id:
                continue
            if record_id in self.refs_in(rec.payload):
                raise SessionStoreError(
                    f"delete forbidden: {rid!r} still refs {record_id!r}"
                )
        del self._data[record_id]
