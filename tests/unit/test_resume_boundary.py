"""Tests for ResumeBoundary on SessionStore."""

from __future__ import annotations

import pytest

from daedalus.host.resume_boundary import (
    GOAL_ID,
    TRANSITION_ID,
    UNRESOLVED_ID,
    ResumeBoundary,
)
from daedalus.host.session_store import Record, SessionStore, SessionStoreError


def test_mark_done_and_read_roundtrip() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    state = boundary.mark_done(
        completed_goal="ship parser v2",
        unresolved_external="await upstream ACK",
    )
    assert state.completed_goal == "ship parser v2"
    assert state.unresolved_external == "await upstream ACK"
    assert boundary.can_resume() is True
    again = boundary.read()
    assert again is not None
    assert again.completed_goal == state.completed_goal
    assert again.unresolved_external == state.unresolved_external
    store.check_invariant()


def test_empty_strings_rejected() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    with pytest.raises(SessionStoreError, match="completed_goal"):
        boundary.mark_done("", "unknown")
    with pytest.raises(SessionStoreError, match="unresolved_external"):
        boundary.mark_done("goal", "  ")
    assert boundary.read() is None
    assert boundary.can_resume() is False


def test_strip_whitespace() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    state = boundary.mark_done("  goal  ", "  unk  ")
    assert state.completed_goal == "goal"
    assert state.unresolved_external == "unk"


def test_transition_blocks_leaf_delete() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    boundary.mark_done("goal-a", "unknown-b")
    with pytest.raises(SessionStoreError, match="still refs"):
        store.delete(GOAL_ID)
    with pytest.raises(SessionStoreError, match="still refs"):
        store.delete(UNRESOLVED_ID)
    boundary.clear()
    assert store.get(TRANSITION_ID) is None
    assert store.get(GOAL_ID) is None
    assert store.get(UNRESOLVED_ID) is None
    assert boundary.can_resume() is False


def test_mark_done_after_clear() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    boundary.mark_done("first", "unk-1")
    boundary.clear()
    state = boundary.mark_done("second", "unk-2")
    assert state.completed_goal == "second"
    assert boundary.can_resume() is True


def test_read_none_without_transition() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    assert boundary.read() is None
    store.put(GOAL_ID, {"kind": "completed_goal", "text": "orphan"})
    assert boundary.read() is None


def test_overwrite_updates_contract() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    boundary.mark_done("first", "unk-1")
    boundary.mark_done("second", "unk-2")
    state = boundary.read()
    assert state is not None
    assert state.completed_goal == "second"
    assert state.unresolved_external == "unk-2"


def test_mark_done_preflight_no_partial_write() -> None:
    store = SessionStore(session="s")
    store._data[UNRESOLVED_ID] = Record(owner="t", payload={"text": "x"})
    boundary = ResumeBoundary(store)
    with pytest.raises(SessionStoreError, match="update forbidden"):
        boundary.mark_done("goal", "unk")
    assert store.get(GOAL_ID) is None
    assert store.get(TRANSITION_ID) is None


def test_foreign_transition_not_readable() -> None:
    store = SessionStore(session="s")
    store._data[TRANSITION_ID] = Record(
        owner="t",
        payload={
            "kind": "resume_transition",
            "refs": [GOAL_ID, UNRESOLVED_ID],
            "completed_goal": "g",
            "unresolved_external": "u",
        },
    )
    store._data[GOAL_ID] = Record(
        owner="t", payload={"kind": "completed_goal", "text": "g"}
    )
    store._data[UNRESOLVED_ID] = Record(
        owner="t", payload={"kind": "unresolved_external", "text": "u"}
    )
    boundary = ResumeBoundary(store)
    assert boundary.read() is None
    assert boundary.can_resume() is False


def test_broken_graph_missing_leaf() -> None:
    store = SessionStore(session="s")
    boundary = ResumeBoundary(store)
    boundary.mark_done("goal", "unk")
    del store._data[GOAL_ID]
    assert boundary.read() is None


def test_clear_rejects_foreign() -> None:
    store = SessionStore(session="s")
    store._data[TRANSITION_ID] = Record(
        owner="t",
        payload={
            "kind": "resume_transition",
            "refs": [],
            "completed_goal": "g",
            "unresolved_external": "u",
        },
    )
    boundary = ResumeBoundary(store)
    with pytest.raises(SessionStoreError, match="delete forbidden"):
        boundary.clear()
