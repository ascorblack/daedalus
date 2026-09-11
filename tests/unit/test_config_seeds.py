"""A seed is applied to a config once; what the operator removes afterwards stays removed."""

from __future__ import annotations

from pathlib import Path

from daedalus.config import RuntimeConfig


def test_the_claude_seed_is_applied_once_and_a_removed_preset_stays_removed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)  # a fresh file: the defaults, seed recorded on the first load below
    assert "claude.opus-5" in config.presets
    # The operator removes every Claude preset and the file is saved without them.
    for pid in [p for p in config.presets if p.startswith("claude.")]:
        del config.presets[pid]
    config.save(path)
    reloaded = RuntimeConfig.load(path)
    assert not [p for p in reloaded.presets if p.startswith("claude.")]
    assert "claude-subscription" in reloaded.seeded
    # And a restart later still has them gone.
    assert not [p for p in RuntimeConfig.load(path).presets if p.startswith("claude.")]
