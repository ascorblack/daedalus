"""A seed is applied to a config once; what the operator removes afterwards stays removed.

A seed exists to give a config written before a feature existed the entries that feature needs. A
file created now is not one of those — it starts with no models at all, by design — so the seeds
count as applied to it from the start.
"""

from __future__ import annotations

from pathlib import Path

import tomli_w

from daedalus.config import RuntimeConfig


def test_a_fresh_config_is_created_with_no_models_and_every_seed_marked_applied(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)
    assert config.presets == {} and config.model.preset == "" and config.has_model is False
    assert "claude-subscription" in config.seeded
    # And a restart does not quietly grow a model table behind the operator's back.
    assert RuntimeConfig.load(path).presets == {}


def test_the_claude_seed_is_applied_once_and_a_removed_preset_stays_removed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    # A config written before the seed existed: presets of its own, no record of any seed.
    with path.open("wb") as fh:
        tomli_w.dump({"presets": {"vllm.m": {"provider": "vllm", "model": "m"}}, "model": {"preset": "vllm.m", "chain": []}}, fh)
    config = RuntimeConfig.load(path)
    assert "claude.opus-5" in config.presets  # the seed brought the subscription models with it
    assert config.model.preset == "vllm.m"  # and left the operator's own default alone
    # The operator removes every Claude preset and the file is saved without them.
    for pid in [p for p in config.presets if p.startswith("claude.")]:
        del config.presets[pid]
    config.save(path)
    reloaded = RuntimeConfig.load(path)
    assert not [p for p in reloaded.presets if p.startswith("claude.")]
    assert "claude-subscription" in reloaded.seeded
    # And a restart later still has them gone.
    assert not [p for p in RuntimeConfig.load(path).presets if p.startswith("claude.")]
