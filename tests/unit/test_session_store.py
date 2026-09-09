"""Session-local store: ownership gate and reference closure (fail-before-write)."""

from __future__ import annotations

import pytest

from daedalus.host.session_store import Record, SessionStore, SessionStoreError


def test_create_sets_owner_to_session_not_caller_field():
    store = SessionStore(session="s")
    # Caller tries to smuggle an owner field inside the payload; host ignores it
    # for ownership and still stamps owner=s on the record.
    store.put("a", {"owner": "t", "value": 1})
    rec = store.get("a")
    assert rec is not None
    assert rec.owner == "s"
    assert rec.payload["owner"] == "t"  # payload key is data, not authority


def test_update_foreign_top_level_owner_rejected_store_unchanged():
    store = SessionStore(session="s")
    store._data["x"] = Record(owner="t", payload={"value": 0})
    before = store.snapshot()
    with pytest.raises(SessionStoreError, match="update forbidden"):
        store.put("x", {"value": 1})
    assert store.snapshot() == before


def test_overwrite_foreign_id_labelling_replacement_owner_s_still_rejected():
    """Negative test: checking only a proposed owner would accept this attack."""
    store = SessionStore(session="s")
    store._data["x"] = Record(owner="t", payload={"value": 0})
    before = store.snapshot()
    # Payload claims owner=s; authorization must use existing.owner, not payload.
    with pytest.raises(SessionStoreError, match="update forbidden"):
        store.put("x", {"owner": "s", "value": 99})
    assert store.snapshot() == before
    assert store.get("x") == Record(owner="t", payload={"value": 0})


def test_nested_foreign_ref_rejected_store_unchanged():
    """Negative test: s-owned payload whose nested ref resolves to t-owned."""
    store = SessionStore(session="s")
    store._data["foreign"] = Record(owner="t", payload={"value": 1})
    before = store.snapshot()
    with pytest.raises(SessionStoreError, match="ref closure"):
        store.put("mine", {"ref": "foreign", "note": "launder"})
    assert store.snapshot() == before
    assert store.get("mine") is None


def test_nested_same_owner_ref_accepted():
    store = SessionStore(session="s")
    store.put("child", {"value": 1})
    store.put("parent", {"ref": "child"})
    assert store.get("parent") == Record(owner="s", payload={"ref": "child"})
    store.check_invariant()


def test_missing_ref_rejected():
    store = SessionStore(session="s")
    before = store.snapshot()
    with pytest.raises(SessionStoreError, match="does not exist"):
        store.put("orphan", {"ref": "nope"})
    assert store.snapshot() == before


def test_refs_list_closure():
    store = SessionStore(session="s")
    store.put("a", {"value": 1})
    store.put("b", {"value": 2})
    store.put("bag", {"refs": ["a", "b"]})
    store.check_invariant()
    store._data["evil"] = Record(owner="t", payload={"value": 9})
    with pytest.raises(SessionStoreError, match="ref closure"):
        store.put("bag2", {"refs": ["a", "evil"]})
    assert store.get("bag2") is None
    assert store.get("bag") == Record(owner="s", payload={"refs": ["a", "b"]})


def test_delete_with_inbound_ref_rejected():
    store = SessionStore(session="s")
    store.put("child", {"value": 1})
    store.put("parent", {"ref": "child"})
    with pytest.raises(SessionStoreError, match="still refs"):
        store.delete("child")
    assert store.get("child") is not None
    assert store.get("parent") is not None


def test_delete_after_parent_gone():
    store = SessionStore(session="s")
    store.put("child", {"value": 1})
    store.put("parent", {"ref": "child"})
    store.delete("parent")
    store.delete("child")
    assert store.get("child") is None


def test_delete_foreign_rejected():
    store = SessionStore(session="s")
    store._data["x"] = Record(owner="t", payload={})
    with pytest.raises(SessionStoreError, match="delete forbidden"):
        store.delete("x")
    assert store.get("x") == Record(owner="t", payload={})


def test_invariant_detects_corruption():
    store = SessionStore(session="s")
    store.put("a", {"value": 1})
    # Bypass put to plant a broken edge
    store._data["b"] = Record(owner="s", payload={"ref": "missing"})
    with pytest.raises(SessionStoreError, match="invariant"):
        store.check_invariant()


def test_caller_nested_mutation_after_put_does_not_break_invariant():
    store = SessionStore(session="s")
    store.put("child", {"value": 1})
    payload = {"nest": {"ref": "child"}}
    store.put("parent", payload)
    store._data["foreign"] = Record(owner="t", payload={"value": 9})
    # Mutate the caller's object after commit — store must stay isolated.
    payload["nest"]["ref"] = "foreign"
    store.check_invariant()
    assert store.get("parent") == Record(owner="s", payload={"nest": {"ref": "child"}})


def test_get_returns_copy_not_live_view():
    store = SessionStore(session="s")
    store.put("a", {"nest": {"n": 1}})
    got = store.get("a")
    assert got is not None
    got.payload["nest"]["n"] = 99
    assert store.get("a") == Record(owner="s", payload={"nest": {"n": 1}})


def test_empty_and_non_str_ids_rejected():
    store = SessionStore(session="s")
    with pytest.raises(SessionStoreError, match="non-empty str"):
        store.put("", {"value": 1})
    with pytest.raises(SessionStoreError, match="non-empty str"):
        store.put(None, {"value": 1})  # type: ignore[arg-type]
    with pytest.raises(SessionStoreError, match="non-empty str"):
        store.delete("")
    with pytest.raises(SessionStoreError, match="non-empty str"):
        store.get("")


def test_tuple_refs_ignored_by_design():
    store = SessionStore(session="s")
    store._data["foreign"] = Record(owner="t", payload={"value": 1})
    # Tuple under refs is not a JSON list — not walked, so no foreign-ref error.
    store.put("bag", {"refs": ("foreign",)})
    assert store.refs_in({"refs": ("foreign",)}) == []
    store.check_invariant()
