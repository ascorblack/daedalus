"""Pure candidate validation and revisioning for mutable runtime configuration."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from daedalus.config import RuntimeConfig


class ConfigProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: Literal["provider", "mcp", "runtime"]
    path: str
    message: str


class EffectiveConfigChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    apply_at: Literal["next_step", "reconnect", "restart"]


class ConfigValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: str
    candidate_revision: str | None = None
    valid: bool
    stale: bool = False
    problems: list[ConfigProblem] = Field(default_factory=list)
    changes: list[EffectiveConfigChange] = Field(default_factory=list)
    required_restart: bool = False
    probes: list[dict[str, Any]] = Field(default_factory=list)


class ConfigConflict(ValueError):
    """The editor based its candidate on a configuration that is no longer current."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("settings changed in another window; review the current values and try again")
        self.current_revision = current_revision


def config_revision(config: RuntimeConfig) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        out.update(_flatten(item, path))
    return out


def _apply_at(path: str) -> Literal["next_step", "reconnect", "restart"]:
    if path.startswith(("providers.", "mcp.")):
        return "reconnect"
    if path.startswith(("telegram.", "ops.")):
        return "restart"
    return "next_step"


def _group(path: str) -> Literal["provider", "mcp", "runtime"]:
    if path.startswith("providers."):
        return "provider"
    if path.startswith("mcp."):
        return "mcp"
    return "runtime"


def validate_candidate(current: RuntimeConfig, raw: dict[str, Any], *, base_revision: str) -> tuple[ConfigValidation, RuntimeConfig | None]:
    current_revision = config_revision(current)
    if base_revision != current_revision:
        return ConfigValidation(base_revision=current_revision, valid=False, stale=True), None
    try:
        candidate = RuntimeConfig.model_validate(raw)
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_url=False):
            path = ".".join(str(part) for part in error["loc"])
            problems.append(ConfigProblem(group=_group(path), path=path, message=str(error["msg"])[:500]))
        return ConfigValidation(base_revision=current_revision, valid=False, problems=problems), None
    before = _flatten(current.model_dump(mode="json"))
    after = _flatten(candidate.model_dump(mode="json"))
    changes = [EffectiveConfigChange(path=path, apply_at=_apply_at(path)) for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]
    return ConfigValidation(
        base_revision=current_revision,
        candidate_revision=config_revision(candidate),
        valid=True,
        changes=changes,
        required_restart=any(change.apply_at == "restart" for change in changes),
    ), candidate


__all__ = ["ConfigConflict", "ConfigProblem", "ConfigValidation", "EffectiveConfigChange", "config_revision", "validate_candidate"]
