"""Boot guard atomic write: per-write temp names, fsync, no orphaned temps."""

from __future__ import annotations

from pathlib import Path

import pytest

import daedalus.host.boot_guard as boot_guard
from daedalus.host.boot_guard import _atomic_write


def test_atomic_write_replaces_content_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "boot-history.json"
    _atomic_write(target, "[]")
    _atomic_write(target, '["2026-09-07T00:00:00+00:00"]')
    assert target.read_text(encoding="utf-8") == '["2026-09-07T00:00:00+00:00"]'
    assert [p.name for p in tmp_path.iterdir()] == ["boot-history.json"]


def test_atomic_write_uses_distinct_temp_names(tmp_path: Path, monkeypatch) -> None:
    """Two writes in one process must not share a temp file (pid-only names collide)."""
    target = tmp_path / "marker"
    names: list[str] = []
    real_replace = boot_guard.os.replace

    def spy(src, dst):
        names.append(src.name)
        return real_replace(src, dst)

    monkeypatch.setattr(boot_guard.os, "replace", spy)
    _atomic_write(target, "1")
    _atomic_write(target, "2")
    assert len(names) == 2 and names[0] != names[1]
    assert all(n.startswith("marker.tmp.") for n in names)


def test_atomic_write_cleans_up_orphan_on_failure(tmp_path: Path, monkeypatch) -> None:
    """A failed replace must not leave a .tmp file behind on disk."""
    target = tmp_path / "marker"

    def boom(src, dst):
        raise PermissionError("simulated")

    monkeypatch.setattr(boot_guard.os, "replace", boom)
    with pytest.raises(PermissionError):
        _atomic_write(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == []
    assert not target.exists()


def test_atomic_write_fsyncs_before_replace(tmp_path: Path, monkeypatch) -> None:
    """The data must hit disk before the rename, or a power cut leaves a 0-byte file."""
    import builtins

    target = tmp_path / "marker"
    order: list[str] = []
    real_replace = boot_guard.os.replace
    real_open = builtins.open

    class _SpyFile:
        def __init__(self, real):
            self._real = real

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

        def write(self, text):
            order.append("write")
            return self._real.write(text)

        def flush(self):
            order.append("flush")
            return self._real.flush()

        def fileno(self):
            return self._real.fileno()

    def spy_open(path, *args, **kwargs):
        return _SpyFile(real_open(path, *args, **kwargs))

    def spy_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(boot_guard.os, "replace", spy_replace)
    monkeypatch.setattr(boot_guard.os, "fsync", lambda fd: order.append("fsync"))
    _atomic_write(target, "x")
    assert order == ["write", "flush", "fsync", "replace"]
    assert target.read_text(encoding="utf-8") == "x"
